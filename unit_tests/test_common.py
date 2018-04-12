#!/usr/bin/python3

"""
A set of unit tests for the storpool-common layer.
"""

import os
import sys
import unittest

import mock

from charmhelpers.core import hookenv

root_path = os.path.realpath('.')
if root_path not in sys.path:
    sys.path.insert(0, root_path)

lib_path = os.path.realpath('unit_tests/lib')
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from spcharms import config as spconfig
from spcharms import txn
from spcharms import utils as sputils


class MockReactive(object):
    def r_clear_states(self):
        self.states = set()

    def __init__(self):
        self.r_clear_states()

    def set_state(self, name):
        self.states.add(name)

    def remove_state(self, name):
        if name in self.states:
            self.states.remove(name)

    def is_state(self, name):
        return name in self.states

    def r_get_states(self):
        return set(self.states)

    def r_set_states(self, states):
        self.states = set(states)


initializing_config = None


class MockConfig(object):
    def r_clear_config(self):
        global initializing_config
        saved = initializing_config
        initializing_config = self
        self.override = {}
        self.changed_attrs = {}
        self.config = {}
        initializing_config = saved

    def __init__(self):
        self.r_clear_config()

    def r_set(self, key, value, changed):
        self.override[key] = value
        self.changed_attrs[key] = changed

    def get(self, key, default):
        return self.override.get(key, self.config.get(key, default))

    def changed(self, key):
        return self.changed_attrs.get(key, False)

    def __getitem__(self, name):
        # Make sure a KeyError is actually thrown if needed.
        if name in self.override:
            return self.override[name]
        else:
            return self.config[name]

    def __getattr__(self, name):
        return self.config.__getattribute__(name)

    def __setattr__(self, name, value):
        if initializing_config == self:
            return super(MockConfig, self).__setattr__(name, value)

        raise AttributeError('Cannot override the MockConfig '
                             '"{name}" attribute'.format(name=name))


r_state = MockReactive()
r_config = MockConfig()

# Do not give hookenv.config() a chance to run at all
hookenv.config = lambda: exit('You just called to say... what?!')
spconfig.m = lambda: r_config


def mock_reactive_states(f):
    def inner1(inst, *args, **kwargs):
        @mock.patch('charms.reactive.set_state', new=r_state.set_state)
        @mock.patch('charms.reactive.remove_state', new=r_state.remove_state)
        @mock.patch('charms.reactive.helpers.is_state', new=r_state.is_state)
        def inner2(*args, **kwargs):
            return f(inst, *args, **kwargs)

        return inner2()

    return inner1


from reactive import storpool_common as testee

INSTALLED_STATE = 'storpool-common.package-installed'
COPIED_STATE = 'storpool-common.config-written'

COMBINED_LINE = 'MemTotal: 20000 M\nprocessor : 0\n'
CGCONFIG_BASE = '/usr/share/doc/storpool/examples/cgconfig/ubuntu1604'
OS_STAT_RESULT = os.stat('/etc/passwd')


class TestStorPoolCommon(unittest.TestCase):
    """
    Test various aspects of the storpool-common layer.
    """
    def setUp(self):
        """
        Clean up the reactive states information between tests.
        """
        super(TestStorPoolCommon, self).setUp()
        r_state.r_clear_states()
        r_config.r_clear_config()
        sputils.err.side_effect = lambda *args: self.fail_on_err(*args)

    def fail_on_err(self, msg):
        self.fail('sputils.err() invoked: {msg}'.format(msg=msg))

    def test_missing_kernel_parameters(self):
        # Missing kernel parameters
        mock_file = mock.mock_open(read_data='no such parameters')
        with mock.patch('reactive.storpool_common.open', mock_file,
                        create=True):
            missing = testee.missing_kernel_parameters()
            self.assertEquals(missing, list(testee.KERNEL_REQUIRED_PARAMS))

        # Correct kernel parameters
        params = list(testee.KERNEL_REQUIRED_PARAMS)
        params.reverse()
        mock_file = mock.mock_open(read_data=' '.join(params))
        with mock.patch('reactive.storpool_common.open', mock_file,
                        create=True):
            missing = testee.missing_kernel_parameters()
            self.assertEquals(missing, [])

    def test_get_total_swap(self):
        mock_file = mock.mock_open(read_data="""
Filename                        Type            Size    Used    Priority
/dev/dm-1                       partition       974844  286332  -2
/dev/dm-2                       partition       74844   6332    -1
""")
        with mock.patch('reactive.storpool_common.open', mock_file,
                        create=True):
            swap = testee.get_total_swap()
            self.assertEquals(swap, int((974844 + 74844) / 1024))

    def test_get_total_memory(self):
        mock_file = mock.mock_open(read_data="""MemTotal:        8053556 kB
MemFree:          145412 kB
MemAvailable:    4145568 kB
Buffers:          270176 kB
Cached:          2448460 kB
SwapCached:        17136 kB
Active:          3442484 kB
""")
        with mock.patch('reactive.storpool_common.open', mock_file,
                        create=True):
            mem = testee.get_total_memory()
            self.assertEquals(mem, int(8053556 / 1024))

        mock_file = mock.mock_open(read_data="""MemTotal:        8053556 MB
""")
        with mock.patch('reactive.storpool_common.open', mock_file,
                        create=True):
            mem = testee.get_total_memory()
            self.assertEquals(mem, 8053556)

        mock_file = mock.mock_open(read_data="""MemTotal:        8053556 GB
""")
        with mock.patch('reactive.storpool_common.open', mock_file,
                        create=True):
            mem = testee.get_total_memory()
            self.assertEquals(mem, 8053556 * 1024)

    @mock_reactive_states
    @mock.patch('charmhelpers.core.host.service_restart')
    def test_copy_config_files(self, service_restart):
        """
        Test that the layer enables the system startup service.
        """
        count_txn_install = txn.install.call_count

        testee.copy_config_files()
        self.assertEqual(count_txn_install + 2, txn.install.call_count)
        service_restart.assert_called_once_with('rsyslog')
        self.assertEquals(set([COPIED_STATE]), r_state.r_get_states())
