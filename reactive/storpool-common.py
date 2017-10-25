from __future__ import print_function

import os
import subprocess

from charms import reactive
from charmhelpers.core import hookenv, host

from spcharms import repo as sprepo
from spcharms import txn
from spcharms import utils as sputils


def rdebug(s):
    sputils.rdebug(s, prefix='common')


@reactive.when('storpool-repo-add.available', 'l-storpool-config.config-written')
@reactive.when_not('storpool-common.package-installed')
@reactive.when_not('storpool-common.stopped')
def install_package():
    rdebug('the common repo has become available and we do have the configuration')

    hookenv.status_set('maintenance', 'obtaining the requested StorPool version')
    spver = hookenv.config().get('storpool_version', None)
    if spver is None or spver == '':
        rdebug('no storpool_version key in the charm config yet')
        return

    hookenv.status_set('maintenance', 'installing the StorPool common packages')
    (err, newly_installed) = sprepo.install_packages({
        'storpool-cli': spver,
        'storpool-common': spver,
        'storpool-etcfiles': spver,
        'kmod-storpool-' + os.uname().release: spver,
        'python-storpool': spver,
    })
    if err is not None:
        rdebug('oof, we could not install packages: {err}'.format(err=err))
        rdebug('removing the package-installed state')
        return

    if newly_installed:
        rdebug('it seems we managed to install some packages: {names}'.format(names=newly_installed))
        sprepo.record_packages('storpool-common', newly_installed)
    else:
        rdebug('it seems that all the packages were installed already')

    rdebug('updating the kernel module dependencies')
    hookenv.status_set('maintenance', 'updating the kernel module dependencies')
    subprocess.check_call(['depmod', '-a'])

    rdebug('setting the package-installed state')
    reactive.set_state('storpool-common.package-installed')
    hookenv.status_set('maintenance', '')


@reactive.when('l-storpool-config.config-written', 'storpool-common.package-installed')
@reactive.when_not('storpool-common.config-written')
@reactive.when_not('storpool-common.stopped')
def copy_config_files():
    hookenv.status_set('maintenance', 'copying the storpool-common config files')
    basedir = '/usr/lib/storpool/etcfiles/storpool-common'
    for f in (
        '/etc/rsyslog.d/99-StorPool.conf',
        '/etc/sysctl.d/99-StorPool.conf',
    ):
        rdebug('installing {fname}'.format(fname=f))
        txn.install('-o', 'root', '-g', 'root', '-m', '644', basedir + f, f)

    rdebug('about to restart rsyslog')
    hookenv.status_set('maintenance', 'restarting the system logging service')
    host.service_restart('rsyslog')

    reactive.set_state('storpool-common.config-written')
    hookenv.status_set('maintenance', '')


@reactive.when('storpool-common.package-installed')
@reactive.when_not('l-storpool-config.config-written')
@reactive.when_not('storpool-common.stopped')
def reinstall():
    reactive.remove_state('storpool-common.package-installed')


@reactive.when('storpool-common.config-written')
@reactive.when_not('storpool-common.package-installed')
@reactive.when_not('storpool-common.stopped')
def rewrite():
    reactive.remove_state('storpool-common.config-written')


def reset_states():
    reactive.remove_state('storpool-common.package-installed')
    reactive.remove_state('storpool-common.config-written')


@reactive.hook('upgrade-charm')
def upgrade():
    rdebug('storpool-common.upgrade-charm invoked')
    reset_states()


@reactive.when('storpool-common.stop')
@reactive.when_not('storpool-common.stopped')
def remove_leftovers():
    rdebug('storpool-common.stop invoked')
    reactive.remove_state('storpool-common.stop')

    rdebug('removing any base StorPool packages')
    sprepo.unrecord_packages('storpool-common')

    rdebug('letting storpool-config know')
    reactive.set_state('l-storpool-config.stop')

    reset_states()
    reactive.set_state('storpool-common.stopped')
