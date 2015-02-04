"""RemoteSpawner implementation"""
import signal
import errno
import pwd
import os
import pipes
from subprocess import Popen

from tornado import gen

from jupyterhub.spawner import Spawner
from IPython.utils.traitlets import (
    Instance, Integer,
)

from jupyterhub.utils import random_port
from jupyterhub.spawner import set_user_setuid

import paramiko

def execute(channel, command):
    """Execute command and get remote PID

    from http://stackoverflow.com/questions/9872872/get-pid-from-paramiko"""
    command = 'echo $$; exec ' + command
    stdin, stdout, stderr = channel.exec_command(command)
    pid = int(stdout.readline())
    return pid, stdin, stdout, stderr

class RemoteSpawner(Spawner):
    """A Spawner that just uses Popen to start local processes."""

    INTERRUPT_TIMEOUT = Integer(10, config=True, \
        help="Seconds to wait for process to halt after SIGINT before proceeding to SIGTERM"
                               )
    TERM_TIMEOUT = Integer(5, config=True, \
        help="Seconds to wait for process to halt after SIGTERM before proceeding to SIGKILL"
                          )
    KILL_TIMEOUT = Integer(5, config=True, \
        help="Seconds to wait for process to halt after SIGKILL before giving up"
                          )

    channel = Instance(paramiko.client.SSHClient)
    pid = Integer(0)

    def make_preexec_fn(self, name):
        """make preexec fn"""
        return set_user_setuid(name)

    def load_state(self, state):
        """load pid from state"""
        super(RemoteSpawner, self).load_state(state)
        if 'pid' in state:
            self.pid = state['pid']

    def get_state(self):
        """add pid to state"""
        state = super(RemoteSpawner, self).get_state()
        if self.pid:
            state['pid'] = self.pid
        return state

    def clear_state(self):
        """clear pid state"""
        super(RemoteSpawner, self).clear_state()
        self.pid = 0

    def user_env(self, env):
        """get user environment"""
        env['USER'] = self.user.name
        env['HOME'] = pwd.getpwnam(self.user.name).pw_dir
        return env

    def _env_default(self):
        env = super()._env_default()
        return self.user_env(env)

    @gen.coroutine
    def start(self):
        """Start the process"""
        self.user.server.port = random_port()
        cmd = []
        env = self.env.copy()

        cmd.extend(self.cmd)
        cmd.extend(self.get_args())

        self.log.debug("Env: %s", str(env))
        self.channel = paramiko.SSHClient()
        self.channel.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.channel.connect("docker3", username="zonca")
        #self.proc = Popen(cmd, env=env, \
        #    preexec_fn=self.make_preexec_fn(self.user.name),
        #                 )
        for k, v in env.items():
            cmd.insert(0, 'export %s="%s";' % (k, v))
        self.log.info("Spawning %s", ' '.join(cmd))
        self.pid, stdin, stdout, stderr = execute(self.channel, ' '.join(cmd))
        #self.log.debug("Error %s", ''.join(stderr.readlines()))

    @gen.coroutine
    def poll(self):
        """Poll the process"""
        # for now just assume it is ok
        return None
        ### if we started the process, poll with Popen
        ##if self.proc is not None:
        ##    status = self.proc.poll()
        ##    if status is not None:
        ##        # clear state if the process is done
        ##        self.clear_state()
        ##    return status

        ### if we resumed from stored state,
        ### we don't have the Popen handle anymore, so rely on self.pid

        ##if not self.pid:
        ##    # no pid, not running
        ##    self.clear_state()
        ##    return 0

        ### send signal 0 to check if PID exists
        ### this doesn't work on Windows, but that's okay because we don't support Windows.
        ##alive = yield self._signal(0)
        ##if not alive:
        ##    self.clear_state()
        ##    return 0
        ##else:
        ##    return None

    @gen.coroutine
    def _signal(self, sig):
        """simple implementation of signal

        we can use it when we are using setuid (we are root)"""
        try:
            os.kill(self.pid, sig)
        except OSError as e:
            if e.errno == errno.ESRCH:
                return False # process is gone
            else:
                raise
        return True # process exists

    @gen.coroutine
    def stop(self, now=False):
        """stop the subprocess

        if `now`, skip waiting for clean shutdown
        """
        if not now:
            status = yield self.poll()
            if status is not None:
                return
            self.log.debug("Interrupting %i", self.pid)
            yield self._signal(signal.SIGINT)
            yield self.wait_for_death(self.INTERRUPT_TIMEOUT)

        # clean shutdown failed, use TERM
        status = yield self.poll()
        if status is not None:
            return
        self.log.debug("Terminating %i", self.pid)
        yield self._signal(signal.SIGTERM)
        yield self.wait_for_death(self.TERM_TIMEOUT)

        # TERM failed, use KILL
        status = yield self.poll()
        if status is not None:
            return
        self.log.debug("Killing %i", self.pid)
        yield self._signal(signal.SIGKILL)
        yield self.wait_for_death(self.KILL_TIMEOUT)

        status = yield self.poll()
        if status is None:
            # it all failed, zombie process
            self.log.warn("Process %i never died", self.pid)
