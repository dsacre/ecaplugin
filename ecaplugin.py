#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# ecaplugin.py
#
# Copyright (C) 2014  Dominic Sacré  <dominic.sacre@gmx.de>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import bs4
import gzip
import argparse
import sys


class LadspaPlugin:
    """
    Description of the parameters of a LADSPA plugin.
    """
    def __init__(self, unique_id, values, enabled=True, name=None):
        self.name = name
        self.enabled = enabled
        self.unique_id = unique_id
        self.values = values

    def format_cmdline(self, format_value=lambda v: v):
        """
        Return the plugin's invocation on the ecasound command line.
        """
        values = ','.join(format_value(v) for v in self.values)
        return '-eli:%d,%s' % (self.unique_id, values)


class LV2Plugin:
    """
    Description of the parameters of an LV2 plugin.
    """
    def __init__(self, uri, values, enabled=True, name=None):
        self.name = name
        self.enabled = enabled
        self.uri = uri
        self.values = values

    def format_cmdline(self, format_value=lambda v: v):
        """
        Return the plugin's invocation on the ecasound command line.
        """
        values = ','.join(format_value(v) for v in self.values)
        return '-elv2:%s,%s' % (self.uri, values)


class Track:
    """
    A sequence of plugins, optionally including a track name.
    """
    def __init__(self, plugins, nchannels=2, samplerate=48000, name=None):
        self.plugins = plugins
        self.nchannels = nchannels
        self.samplerate = samplerate
        self.name = name


def serve_soup(data):
    """
    Turn data into soup, unless we already have soup.
    """
    if isinstance(data, bs4.BeautifulSoup):
        return data
    else:
        return bs4.BeautifulSoup(data, 'xml')


class Ardour2Session:
    """
    Extract plugin parameters from an Ardour 2.x session.
    """
    def __init__(self, data):
        soup = serve_soup(data)
        self.tracks = []

        # find and parse all routes (tracks and busses) in the session
        for route in soup.Session.Routes.find_all('Route'):
            self.tracks.append(self.parse_route(soup, route))

    def parse_route(self, soup, route):
        if route.IO.has_attr('input-connection'):
            # this may not be quite right
            nchannels = 2 if '+' in route.IO['input-connection'] else 1
        else:
            nchannels = route.IO['inputs'].count('{')
        samplerate = int(soup.Session['sample-rate'])
        name = route.IO['name']

        # pre-fader and post-fader are mixed, but the order within each of
        # these sets seems to define their order in the channel strip
        pre_fader = []
        post_fader = []
        for insert in route.find_all('Insert'):
            type = insert['type']
            is_post_fader = insert.Redirect['placement'] == 'PostFader'
            if type == 'ladspa':
                plugin = self.parse_ladspa(insert)
            elif type == 'lv2':
                plugin = self.parse_lv2(insert)
            else:
                continue
            (post_fader if is_post_fader else pre_fader).append(plugin)

        # output all pre-fader plugins before any post-fader plugins
        return Track(pre_fader + post_fader, nchannels, samplerate, name)

    def parse_ladspa(self, insert):
        enabled = insert.Redirect['active'] == 'yes'
        name = insert.Redirect.IO['name']
        unique_id = int(insert['unique-id'])
        values = [p['value'] for p in insert.ladspa.find_all('port')]
        return LadspaPlugin(unique_id, values, enabled, name)

    def parse_lv2(self, insert):
        enabled = insert.Redirect['active'] == 'yes'
        name = insert.Redirect.IO['name']
        uri = insert['unique-id']
        values = [p['value'] for p in insert.lv2.find_all('port')]
        return LV2Plugin(uri, values, enabled, name)


class Ardour3Session:
    """
    Extract plugin parameters from an Ardour 3.x session.
    """
    def __init__(self, data):
        soup = serve_soup(data)
        self.tracks = []

        # find and parse all routes (tracks and busses) in the session
        for route in soup.Session.Routes.find_all('Route'):
            self.tracks.append(self.parse_route(soup, route))

    def parse_route(self, soup, route):
        nchannels = len(route.find('IO', direction='Input').find_all('Port'))
        samplerate = int(soup.Session['sample-rate'])
        name = route.IO['name']

        plugins = []
        for processor in route.find_all('Processor'):
            type = processor['type']
            if type == 'ladspa':
                plugins.append(self.parse_ladspa(processor))
            elif type == 'lv2':
                plugins.append(self.parse_lv2(processor))
            else:
                continue

        return Track(plugins, nchannels, samplerate, name)

    def parse_ladspa(self, processor):
        enabled = processor['active'] == 'yes'
        name = processor['name']
        unique_id = int(processor['unique-id'])
        values = [p['value'] for p in processor.ladspa.find_all('Port')]
        return LadspaPlugin(unique_id, values, enabled, name)

    def parse_lv2(self, processor):
        enabled = processor['active'] == 'yes'
        name = processor['name']
        uri = processor['unique-id']
        values = [p['value'] for p in processor.lv2.find_all('Port')]
        return LV2Plugin(uri, values, enabled, name)


class JackRack:
    """
    Extract plugin information from JACK Rack.
    """
    def __init__(self, data):
        soup = serve_soup(data)

        nchannels = int(soup.jackrack.channels.string)
        samplerate = int(soup.jackrack.samplerate.string)

        plugins = []
        for plugin in soup.jackrack.find_all('plugin'):
            plugins.append(self.parse_ladspa(plugin))

        self.track = Track(plugins, nchannels, samplerate)

    def parse_ladspa(self, plugin):
        enabled = plugin.enabled.string == 'true'
        unique_id = int(plugin.id.string)
        values = [cr.value.string for cr in plugin.find_all('controlrow')]
        return LadspaPlugin(unique_id, values, enabled)


class EcasoundOutput:
    """
    Output plugin invocations in ecasound command line format.
    """
    def __init__(self, args):
        vars(self).update(vars(args))

    def format_session(self, tracks):
        """
        Format all given tracks.
        """
        return '\n\n'.join(
            '%s:\n%s\n%s' % (
                track.name,
                '-' * (len(track.name) + 1),
                self.format_track(track)
            )
            for track in tracks
        )

    def format_track(self, track):
        """
        Format the plugins of the given track.
        """
        # check if plugin indices are valid for the given track
        for i in self.include_indices + self.exclude_indices:
            if i < 0 or i >= len(track.plugins):
                sys.exit("error: plugin index %d is out of range" % i)
        if self.include_indices:
            indices = self.include_indices
        else:
            indices = [i for i in range(len(track.plugins))
                            if i not in self.exclude_indices]

        # make list of plugins
        plugins = []
        for i in indices:
            p = track.plugins[i]
            if p.enabled or self.include_disabled:
                plugins.append((i, p))

        separator = ' ' if self.single_line else '\n'
        string = separator.join(self.format_plugin(i, p) for i, p in plugins)

        if self.chain_setup:
            return '-f:f32,%d,%d -G:jack,%s,notransport -i:jack -o:jack\n\n%s' % (
                track.nchannels,
                track.samplerate,
                self.client_name,
                string,
            )
        else:
            return string

    def format_plugin(self, index, plugin):
        """
        Format a single plugin, possibly including its index and name.
        """
        description = ('# %d: %s\n' % (index, (plugin.name if plugin.name else ''))
                       if not self.no_description else '')

        comment = '# ' if not plugin.enabled else ''

        return (description + comment +
                plugin.format_cmdline(self.format_value))

    def format_value(self, value):
        """
        Return the value's shortest possible string representation without
        losing precision.
        """
        floating = float(value)
        integer = int(float(value))
        if float(integer) == floating:
            return str(integer)
        else:
            return str(floating)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Build ecasound command line arguments for LADSPA and LV2 plugins."
    )
    parser.add_argument('filename', type=str, metavar='FILE',
                        help="Ardour or JACK Rack file name")
    parser.add_argument('-c', '--chain-setup', type=str, nargs='?', metavar='CLIENTNAME',
                        action='append', dest='chain_setup_params',
                        help="output complete ecasound chain setup (.ecs)")
    parser.add_argument('-s', '--single-line', action='store_true',
                        help="output on single line (implies -c)")
    parser.add_argument('-n', '--no-description', action='store_true',
                        help="do not output plugin descriptions as comments")
    parser.add_argument('-t', '--track', type=str, metavar='NAME', dest='track_name',
                        help="name of single track/bus to be exported")
    parser.add_argument('-i', '--include', type=int, metavar='INDEX',
                        dest='include_indices', default=[], action='append',
                        help="indices of plugins to be exported (zero-based, default: all)")
    parser.add_argument('-e', '--exclude', type=int, metavar='INDEX',
                        dest='exclude_indices', default=[], action='append',
                        help="indices of plugins not to be exported (zero-based)")
    parser.add_argument('-d', '--include-disabled', action='store_true',
                        help="include disabled plugins as comments")
    try:
        args = parser.parse_args()
    except IOError as ex:
        sys.exit(ex)

    if args.single_line:
        args.no_description = True
        args.include_disabled = False
    args.chain_setup = bool(args.chain_setup_params)
    args.client_name = args.chain_setup_params[0] if \
        args.chain_setup_params and args.chain_setup_params[0] is not None else 'ecasound'

    output = EcasoundOutput(args)

    try:
        # extract gzipped files (JACK Rack). for non-gzipped files, exceptions
        # are not thrown until trying to read from the file.
        data = gzip.GzipFile(args.filename).read()
    except IOError:
        data = open(args.filename)

    # load XML input data
    soup = bs4.BeautifulSoup(data, 'xml')

    # determine input file format
    if soup.find('Session', recursive=False):
        input_format = 'ardour'
        single_track = bool(args.track_name)
    elif soup.find('jackrack', recursive=False):
        input_format = 'jackrack'
        single_track = True
    else:
        sys.exit("error: input file format not recognized")

    # some more argument checking
    if args.chain_setup and not single_track:
        sys.exit("error: ecasound chain setup can only be generated for a single track")
    if args.include_indices and args.exclude_indices:
        sys.exit("error: can't specify plugin inclusion and exclusion at the same time")
    if (args.include_indices or args.exclude_indices) and not single_track:
        sys.exit("error: can't specify plugin indices when exporting whole session")

    if input_format == 'ardour':
        session = Ardour2Session(soup) if soup.Session['version'].startswith('2') \
             else Ardour3Session(soup)

        if args.track_name:
            # output single track
            try:
                print(output.format_track(
                    next(t for t in session.tracks if t.name == args.track_name)
                ))
            except StopIteration:
                sys.exit("error: no track named '%s'" % args.track_name)
        else:
            # output all tracks
            print(output.format_session(session.tracks))

    elif input_format == 'jackrack':
        rack = JackRack(soup)
        print(output.format_track(rack.track))
