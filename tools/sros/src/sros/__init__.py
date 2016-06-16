from __future__ import print_function

import argparse
import os
import subprocess
import sys
import rosgraph.security as security
import shutil

def get_parser():

    class CondAction(argparse.Action):
        def __init__(self, option_strings, dest, nargs=None, **kwargs):
            x = kwargs.pop('to_be_required', [])
            super(CondAction, self).__init__(option_strings, dest, **kwargs)
            self.make_required = x
            self.dest = dest

        def __call__(self, parser, namespace, values, option_string=None):
            for x in self.make_required:
                x.required = True
            setattr(namespace, self.dest, values)

    parser = argparse.ArgumentParser(prog='sroscore',
                                     description='secure roscore')
    parser.add_argument('-k','--keyserver',
                        action='store_true',
                        help='enable keyserver')
    graph_argument = parser.add_argument('-g','--graph',
                        action='store',
                        help='access control graph')
    parser.add_argument('-m','--mode',
                        action=CondAction,
                        to_be_required=[parser._option_string_actions['--graph']],
                        help='access control mode (audit|complain|[enforce]|train)')
    parser.add_argument('--version',
                        action='version',
                        version='%(prog)s 0.0')
    return parser

def roscore_main(argv = sys.argv):
    parser = get_parser()
    args, roscore_argv = parser.parse_known_args(argv)
    print('roscore_argv: ', roscore_argv)

    if args.keyserver:
        # if we're in setup mode, we need to start an unsecured server that will
        # hand out the SSL certificates and keys so that nodes can talk to roscore
        os.environ['ROS_SECURITY'] = 'ssl_setup'
        security.fork_xmlrpc_keyserver()
    else:
        os.environ['ROS_SECURITY'] = 'ssl'

    if args.graph is not None:
        os.environ['ROS_GRAPH_NAME'] = args.graph
    if args.mode is not None:
        os.environ['ROS_GRAPH_MODE'] = args.mode
    else:
        os.environ['ROS_GRAPH_MODE'] = 'enforce'
    
    import roslaunch
    roslaunch.main(['roscore', '--core'] + roscore_argv[1:])

def usage(subcmd=None):
    if subcmd == 'keys':
        print("""
usage: sros keys SUBCOMMAND [OPTIONS]

where SUBCOMMAND is one of:
  delete\n""")
    else:
        print("""
usage: sros COMMAND [OPTIONS]

where COMMAND is one of:
  keys

command-specific help can be obtained by:

sros COMMAND --help\n""")
        return 1

def sros_main(argv = sys.argv):
    if len(argv) <= 1 or argv[1] == '--help':
        return usage()
    cmd = argv[1]
    if cmd == 'keys':
        return sros_keys(argv)
    else:
        return usage()

def sros_keys(argv):
    if len(argv) <= 2 or argv[2] == '--help':
        return usage('keys')
    subcmd = argv[2]
    if subcmd == 'delete':
        return sros_keys_delete(argv)
    else:
        return usage('keys')

def sros_keys_delete(argv):
    # todo: parameterize keystore location, if we expand to multiple keyrings
    keypath = os.path.join(os.path.expanduser('~'), '.ros', 'keys')
    # do a tiny bit of sanity check...
    if keypath.count('/') <= 1:
        print("woah. I probably shouldn't delete this path: [%s]" % keypath)
        sys.exit(1)
    print("deleting keystore directory: [%s]" % keypath)
    shutil.rmtree(keypath)
    print("done.")
    return 0

