"""
A Juju charm layer that installs the base StorPool packages.
"""
from __future__ import print_function

import os
import subprocess
import tempfile

from charms import reactive
from charmhelpers.core import hookenv, host, templating

from spcharms import config as spconfig
from spcharms import repo as sprepo
from spcharms import states as spstates
from spcharms import status as spstatus
from spcharms import txn
from spcharms import utils as sputils

STATES_REDO = {
    'set': [],
    'unset': [
        'storpool-common.package-installed',
        'storpool-common.config-written',
    ],
}


KERNEL_REQUIRED_PARAMS = (
    'swapaccount=1',
    'vga=normal',
    'nofb',
    'nomodeset',
    'video=vesafb:off',
    'i915.modeset=0',
)


def rdebug(s):
    """
    Pass the diagnostic message string `s` to the central diagnostic logger.
    """
    sputils.rdebug(s, prefix='common')


@reactive.when('storpool-helper.config-set')
@reactive.when('storpool-repo-add.available')
@reactive.when('l-storpool-config.package-installed')
@reactive.when_not('storpool-common.package-installed')
@reactive.when_not('storpool-common.stopped')
def install_package():
    """
    Install the base StorPool packages.
    """
    rdebug('the common repo has become available and '
           'we do have the configuration')

    rdebug('checking the kernel command line')
    with open('/proc/cmdline', mode='r') as f:
        ln = f.readline()
        if not ln:
            sputils.err('Could not read a single line from /proc/cmdline')
            return
        words = ln.split()

        # OK, so this is a bit naive, but it will do the job
        global KERNEL_REQUIRED_PARAMS
        missing = list(filter(lambda param: param not in words,
                              KERNEL_REQUIRED_PARAMS))
        if missing:
            if sputils.bypassed('kernel_parameters'):
                hookenv.log('The "kernel_parameters" bypass is meant FOR '
                            'DEVELOPMENT ONLY!  DO NOT run a StorPool cluster '
                            'in production with it!', hookenv.WARNING)
            else:
                sputils.err('Missing kernel parameters: {missing}'
                            .format(missing=' '.join(missing)))
                return

    spstatus.npset('maintenance', 'obtaining the requested StorPool version')
    spver = spconfig.m().get('storpool_version', None)
    if spver is None or spver == '':
        rdebug('no storpool_version key in the charm config yet')
        return

    spstatus.npset('maintenance', 'installing the StorPool common packages')
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
    spstatus.npset('maintenance', 'updating the kernel module dependencies')
    subprocess.check_call(['depmod', '-a'])

    rdebug('gathering CPU information for the cgroup configuration')
    with open('/proc/cpuinfo', mode='r') as f:
        lns = f.readlines()
        all_cpus = sorted(map(lambda lst: int(lst[2]),
                              filter(lambda lst: lst and lst[0] == 'processor',
                                     map(lambda s: s.split(), lns))))
    if sputils.bypassed('very_few_cpus'):
        hookenv.log('The "very_few_cpus" bypass is meant '
                    'FOR DEVELOPMENT ONLY!  DO NOT run a StorPool cluster in '
                    'production with it!', hookenv.WARNING)
        last_cpu = all_cpus[-1]
        all_cpus.extend([last_cpu, last_cpu, last_cpu])
    if len(all_cpus) < 4:
        sputils.err('Not enough CPUs, need at least 4')
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
                sputils.err('Could not find MemTotal in /proc/meminfo')
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
                    sputils.err('Could not parse the "{u}" unit for '
                                'MemTotal in /proc/meminfo'.format(u=words[2]))
                    return
                break
    mem_system = 4 * 1024
    mem_user = 4 * 1024
    mem_storpool = 1 * 1024
    mem_kernel = 10 * 1024
    if sputils.bypassed('very_little_memory'):
        hookenv.log('The "very_little_memory" bypass is meant '
                    'FOR DEVELOPMENT ONLY!  DO NOT run a StorPool cluster in '
                    'production with it!', hookenv.WARNING)
        mem_system = 1 * 1900
        mem_user = 1 * 512
        mem_storpool = 1 * 1024
        mem_kernel = 1 * 512
    mem_reserved = mem_system + mem_user + mem_storpool + mem_kernel
    if mem_total <= mem_reserved:
        sputils.err('Not enough memory, only have {total}M, need {mem}M'
                    .format(mem=mem_reserved, total=mem_total))
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

    rdebug('starting the cgconfig service')
    rdebug('- refreshing the systemctl service database')
    subprocess.check_call(['systemctl', 'daemon-reload'])
    rdebug('- starting the cgconfig service')
    try:
        host.service_resume('cgconfig')
    except Exception:
        pass

    rdebug('setting the package-installed state')
    reactive.set_state('storpool-common.package-installed')
    spstatus.npset('maintenance', '')


@reactive.when('l-storpool-config.config-written',
               'storpool-common.package-installed')
@reactive.when_not('storpool-common.config-written')
@reactive.when_not('storpool-common.stopped')
def copy_config_files():
    """
    Install some configuration files.
    """
    spstatus.npset('maintenance', 'copying the storpool-common config files')
    basedir = '/usr/lib/storpool/etcfiles/storpool-common'
    for f in (
        '/etc/rsyslog.d/99-StorPool.conf',
        '/etc/sysctl.d/99-StorPool.conf',
    ):
        rdebug('installing {fname}'.format(fname=f))
        txn.install('-o', 'root', '-g', 'root', '-m', '644', basedir + f, f)

    rdebug('about to restart rsyslog')
    spstatus.npset('maintenance', 'restarting the system logging service')
    host.service_restart('rsyslog')

    reactive.set_state('storpool-common.config-written')
    spstatus.npset('maintenance', '')


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


@reactive.hook('install')
def register():
    """
    Register our hook state mappings.
    """
    spstates.register('storpool-common', {'upgrade-charm': STATES_REDO})


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

    reactive.set_state('storpool-common.stopped')
    for state in STATES_REDO['set'] + STATES_REDO['unset']:
        reactive.remove_state(state)
