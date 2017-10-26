"""
A Juju charm layer that installs the base StorPool packages.
"""
from __future__ import print_function

import os
import subprocess
import tempfile

from charms import reactive
from charmhelpers.core import hookenv, host, templating

from spcharms import repo as sprepo
from spcharms import txn
from spcharms import utils as sputils


def rdebug(s):
    """
    Pass the diagnostic message string `s` to the central diagnostic logger.
    """
    sputils.rdebug(s, prefix='common')


@reactive.when('storpool-repo-add.available',
               'l-storpool-config.package-installed')
@reactive.when_not('storpool-common.package-installed')
@reactive.when_not('storpool-common.stopped')
def install_package():
    """
    Install the base StorPool packages.
    """
    rdebug('the common repo has become available and '
           'we do have the configuration')

    hookenv.status_set('maintenance',
                       'obtaining the requested StorPool version')
    spver = hookenv.config().get('storpool_version', None)
    if spver is None or spver == '':
        rdebug('no storpool_version key in the charm config yet')
        return

    hookenv.status_set('maintenance',
                       'installing the StorPool common packages')
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
        rdebug('it seems we managed to install some packages: {names}'
               .format(names=newly_installed))
        sprepo.record_packages('storpool-common', newly_installed)
    else:
        rdebug('it seems that all the packages were installed already')

    rdebug('updating the kernel module dependencies')
    hookenv.status_set('maintenance',
                       'updating the kernel module dependencies')
    subprocess.check_call(['depmod', '-a'])

    bypassed_checks = set(hookenv.config().get('bypassed_checks', '').split())
    rdebug('gathering CPU information for the cgroup configuration')
    with open('/proc/cpuinfo', mode='r') as f:
        lns = f.readlines()
        all_cpus = sorted(map(lambda lst: int(lst[2]),
                              filter(lambda lst: lst and lst[0] == 'processor',
                                     map(lambda s: s.split(), lns))))
    if 'very_few_cpus' in bypassed_checks:
        last_cpu = all_cpus[-1]
        all_cpus.extend([last_cpu, last_cpu, last_cpu])
    if len(all_cpus) < 4:
        msg = 'Not enough CPUs, need at least 4'
        hookenv.log(msg, hookenv.ERROR)
        hookenv.status_set('maintenance', msg)
        return
    tdata = {
        'cpu_rdma': str(all_cpus[0]),
        'cpu_beacon': str(all_cpus[1]),
        'cpu_block': str(all_cpus[2]),
        'cpu_rest': '{min}-{max}'.format(min=all_cpus[3], max=all_cpus[-1]),
    }

    rdebug('gathering system memory information for the cgroup configuration')
    with open('/proc/meminfo', mode='r') as f:
        while True:
            line = f.readline()
            if not line:
                msg = 'Could not find MemTotal in /proc/meminfo'
                hookenv.log(msg, hookenv.ERROR)
                hookenv.status_set('maintenance', msg)
                return
            words = line.split()
            if words[0] == 'MemTotal:':
                mem_total = int(words[1])
                unit = words[2].upper()
                if unit.startswith('K'):
                    mem_total = int(mem_total / 1024)
                elif unit.startswith('M'):
                    pass
                elif unit.startswith('G'):
                    mem_total = mem_total * 1024
                else:
                    msg = 'Could not parse the "{u}" unit for MemTotal in ' \
                          '/proc/meminfo'.format(u=words[2])
                    hookenv.log(msg, hookenv.ERROR)
                    hookenv.status_set('maintenance', msg)
                    return
                break
    mem_system = 4 * 1024
    mem_user = 4 * 1024
    mem_storpool = 1 * 1024
    mem_kernel = 10 * 1024
    if 'very_little_memory' in bypassed_checks:
        mem_system = 4 * 102
        mem_user = 4 * 102
        mem_storpool = 1 * 102
        mem_kernel = 10 * 102
    mem_reserved = mem_system + mem_user + mem_storpool + mem_kernel
    if mem_total <= mem_reserved:
        msg = 'Not enough memory, only have {total}M, need {mem}M' \
            .format(mem=mem_reserved, total=mem_total)
        hookenv.log(msg, hookenv.ERROR)
        hookenv.status_set('maintenance', msg)
        return
    mem_machine = mem_total - mem_reserved
    tdata.update({
        'mem_system': mem_system,
        'mem_user': mem_user,
        'mem_storpool': mem_storpool,
        'mem_machine': mem_machine,
    })

    rdebug('generating the cgroup configuration: {tdata}'.format(tdata=tdata))
    if not os.path.isdir('/etc/cgconfig.d'):
        os.mkdir('/etc/cgconfig.d', mode=0o755)
    cgconfig_dir = '/usr/share/doc/storpool/examples/cgconfig/ubuntu1604'
    for (path, _, files) in os.walk(cgconfig_dir):
        for fname in files:
            src = path + '/' + fname
            dst = src.replace(cgconfig_dir, '')
            dstdir = os.path.dirname(dst)
            if not os.path.isdir(dstdir):
                os.makedirs(dstdir, mode=0o755)

            if fname in (
                         'machine.slice.conf',
                         'storpool.slice.conf',
                         'system.slice.conf',
                         'user.slice.conf',
                         'machine-cgsetup.conf',
                        ):
                with tempfile.NamedTemporaryFile(dir='/tmp',
                                                 mode='w+t',
                                                 delete=True) as tempf:
                    rdebug('- generating {tempf} for {dst}'
                           .format(dst=dst, tempf=tempf.name))
                    templating.render(
                                      source=fname,
                                      target=tempf.name,
                                      owner='root',
                                      perms=0o644,
                                      context=tdata,
                                     )
                    rdebug('- generating {dst}'.format(dst=dst))
                    txn.install('-o', 'root', '-g', 'root', '-m', '644', '--',
                                tempf.name, dst)
            else:
                mode = '{:o}'.format(os.stat(src).st_mode & 0o777)
                rdebug('- installing {src} as {dst}'.format(src=src, dst=dst))
                txn.install('-o', 'root', '-g', 'root', '-m', mode, '--',
                            src, dst)

    rdebug('setting the package-installed state')
    reactive.set_state('storpool-common.package-installed')
    hookenv.status_set('maintenance', '')


@reactive.when('l-storpool-config.config-written',
               'storpool-common.package-installed')
@reactive.when_not('storpool-common.config-written')
@reactive.when_not('storpool-common.stopped')
def copy_config_files():
    """
    Install some configuration files.
    """
    hookenv.status_set('maintenance',
                       'copying the storpool-common config files')
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
    """
    Trigger a reinstallation of the StorPool packages.
    """
    reactive.remove_state('storpool-common.package-installed')


@reactive.when('storpool-common.config-written')
@reactive.when_not('storpool-common.package-installed')
@reactive.when_not('storpool-common.stopped')
def rewrite():
    """
    Trigger a recheck of the StorPool configuration files.
    """
    reactive.remove_state('storpool-common.config-written')


def reset_states():
    """
    Trigger a full reinstall-rewrite cycle.
    """
    reactive.remove_state('storpool-common.package-installed')
    reactive.remove_state('storpool-common.config-written')


@reactive.hook('upgrade-charm')
def upgrade():
    """
    Go through a reinstall-rewrite cycle upon charm upgrade.
    """
    rdebug('storpool-common.upgrade-charm invoked')
    reset_states()


@reactive.when('storpool-common.stop')
@reactive.when_not('storpool-common.stopped')
def remove_leftovers():
    """
    Clean up, remove the config files, uninstall the packages.
    """
    rdebug('storpool-common.stop invoked')
    reactive.remove_state('storpool-common.stop')

    rdebug('removing any base StorPool packages')
    sprepo.unrecord_packages('storpool-common')

    rdebug('letting storpool-config know')
    reactive.set_state('l-storpool-config.stop')

    reset_states()
    reactive.set_state('storpool-common.stopped')
