# **************************************************************************
# *
# * Authors:     J.M. De la Rosa Trevin (jmdelarosa@cnb.csic.es)
# *
# * Unidad de  Bioinformatica of Centro Nacional de Biotecnologia , CSIC
# *
# * This program is free software; you can redistribute it and/or modify
# * it under the terms of the GNU General Public License as published by
# * the Free Software Foundation; either version 3 of the License, or
# * (at your option) any later version.
# *
# * This program is distributed in the hope that it will be useful,
# * but WITHOUT ANY WARRANTY; without even the implied warranty of
# * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# * GNU General Public License for more details.
# *
# * You should have received a copy of the GNU General Public License
# * along with this program; if not, write to the Free Software
# * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA
# * 02111-1307  USA
# *
# *  All comments concerning this program package may be sent to the
# *  e-mail address 'scipion@cnb.csic.es'
# *
# **************************************************************************
"""
This module is responsible for launching protocol executions.
There are two main scenarios: local execution and remote execution.

A. Local execution: 
This will depend on the 'localhost' configuration
1- Check if the protocol will be launched with MPI or not (using MPI template from config)
2- Check if the protocol will be submitted to a queue (using Queue template from config)
3- Build the command that will be launched.

B. Remote execution:
1- Establish a connection with remote host for protocol execution
2- Copy necessary files to remote host.
3- Run a local process (for local execution, see case A) in the remote host
4- Get the result back after launching remotely
"""

import os
import re
from subprocess import Popen, PIPE
import pyworkflow as pw
from pyworkflow.utils import (redStr, greenStr, makeFilePath, join, process,
                              getHostFullName)
from pyworkflow.protocol.constants import UNKNOWN_JOBID

LOCALHOST = 'localhost'


# ******************************************************************
# *         Public functions provided by the module
# ******************************************************************

def launch(protocol, wait=False, stdin=None, stdout=None, stderr=None):
    """ This function should be used to launch a protocol
    This function will decide which case, A or B will be used.
    """
    if _isLocal(protocol):
        jobId = _launchLocal(protocol, wait, stdin, stdout, stderr)
    else:
        jobId = _launchRemote(protocol, wait)

    protocol.setJobId(jobId)

    return jobId


def stop(protocol):
    """ 
    """
    if _isLocal(protocol):
        return _stopLocal(protocol)
    else:
        return _stopRemote(protocol)


def schedule(protocol, initialSleepTime=0, wait=False):
    """ Use this function to schedule protocols that are not ready to
    run yet. Right now it only make sense to schedule jobs locally.
    """
    cmd = '%s %s' % (pw.PYTHON, pw.getScheduleScript())
    cmd += ' "%s" "%s" %s --initial_sleep %s' % (protocol.getProject().path,
                              protocol.getDbPath(),
                              protocol.strId(), initialSleepTime)
    jobId = _run(cmd, wait)
    protocol.setJobId(jobId)

    return jobId


# ******************************************************************
# *         Internal utility functions
# ******************************************************************
def _isLocal(protocol):
    return protocol.getHostName() == LOCALHOST


def _runsLocally(protocol):
    """ Return True if this protocol is running in this machine,
    where the PID makes sense.
    """
    return protocol.getHostFullName() == getHostFullName()


# ******************************************************************
# *                 Function related to LAUNCH
# ******************************************************************
def _getAppsProgram(prog):
    """ Get a command to launch a program under the apps folder.
    """
    return "%s %s" % (pw.PYTHON, pw.join(pw.APPS, prog))


def _launchLocal(protocol, wait, stdin=None, stdout=None, stderr=None):
    # Check first if we need to launch with MPI or not
    command = ('%s %s "%s" "%s" %s'
               % (pw.PYTHON, pw.join(pw.APPS, 'pw_protocol_run.py'),
                  protocol.getProject().path, protocol.getDbPath(),
                  protocol.strId()))
    hostConfig = protocol.getHostConfig()
    useQueue = protocol.useQueue()
    # Check if need to submit to queue    
    if useQueue and (protocol.getSubmitDict()["QUEUE_FOR_JOBS"] == "N"):
        submitDict = dict(hostConfig.getQueuesDefault())
        submitDict.update(protocol.getSubmitDict())
        submitDict['JOB_COMMAND'] = command
        jobId = _submit(hostConfig, submitDict)
    else:
        jobId = _run(command, wait, stdin, stdout, stderr)

    return jobId


def _runRemote(protocol, mode):
    """ Launch remotely 'pw_protocol_remote.py' script to run or stop a protocol. 
    Params:
        protocol: the protocol to be ran or stopped.
        mode: should be either 'run' or 'stop'
    """
    host = protocol.getHostConfig()
    tpl = "ssh %(address)s '%(scipion)s/scipion "

    if host.getScipionConfig() is not None:
        tpl += "--config %(config)s "

    tpl += "runprotocol pw_protocol_remote.py %(mode)s "
    tpl += "%(project)s %(protDb)s %(protId)s' "

    # Use project base name,
    # in remote SCIPION_USER_DATA/projects should be prepended
    projectPath = os.path.basename(protocol.getProject().path)

    args = {'address': host.getAddress(),
            'mode': mode,
            'scipion': host.getScipionHome(),
            'config': host.getScipionConfig(),
            'project': projectPath,
            'protDb': protocol.getDbPath(),
            'protId': protocol.getObjId()
            }
    cmd = tpl % args
    print("** Running remote: %s" % greenStr(cmd))
    p = Popen(cmd, shell=True, stdout=PIPE)

    return p


def _launchRemote(protocol, wait):
    p = _runRemote(protocol, 'run')
    jobId = UNKNOWN_JOBID
    out, err = p.communicate()
    if err:
        raise Exception(err)
    s = re.search('Scipion remote jobid: (\d+)', out)
    if s:
        jobId = int(s.group(1))
    else:
        raise Exception("** Couldn't parse ouput: %s" % redStr(out))

    return jobId


def _copyFiles(protocol, rpath):
    """ Copy all required files for protocol to run
    in a remote execution host.
    NOTE: this function should always be execute with 
    the current working dir pointing to the project dir.
    And the remotePath is assumed to be in protocol.getHostConfig().getHostPath()
    Params:
        protocol: protocol to copy files
        ssh: an ssh connection to copy the files.
    """
    remotePath = protocol.getHostConfig().getHostPath()

    for f in protocol.getFiles():
        remoteFile = join(remotePath, f)
        rpath.putFile(f, remoteFile)


def _submit(hostConfig, submitDict, cwd=None, env=None):
    """ Submit a protocol to a queue system. Return its job id.
    """
    # Create first the submission script to be launched
    # formatting using the template
    template = hostConfig.getSubmitTemplate() % submitDict
    # FIXME: CREATE THE PATH FIRST
    scripPath = submitDict['JOB_SCRIPT']
    f = open(scripPath, 'w')
    # Ensure the path exists
    makeFilePath(scripPath)
    # Add some line ends because in some clusters it fails
    # to submit jobs if the submit script does not have end of line
    f.write(template + '\n\n')
    f.close()
    # This should format the command using a template like: 
    # "qsub %(JOB_SCRIPT)s"
    command = hostConfig.getSubmitCommand() % submitDict
    gcmd = greenStr(command)
    print("** Submitting to queue: '%s'" % gcmd)

    p = Popen(command, shell=True, stdout=PIPE, cwd=cwd, env=env)
    out = p.communicate()[0]
    # Try to parse the result of qsub, searching for a number (jobId)
    # Review this, seems to exclusive to torque batch system
    s = re.search('(\d+)', str(out))
    if p.returncode == 0 and s:
        job = int(s.group(0))
        print("Launched job with id %s" % job)
        return job
    else:
        print("Couldn't submit to queue for reason: %s " % redStr(out.decode()))
        return UNKNOWN_JOBID


def _run(command, wait, stdin=None, stdout=None, stderr=None):
    """ Execute a command in a subprocess and return the pid. """
    gcmd = greenStr(command)
    print("** Running command: '%s'" % gcmd)
    p = Popen(command, shell=True, stdout=stdout, stderr=stderr)
    jobId = p.pid
    if wait:
        p.wait()

    return jobId


# ******************************************************************
# *                 Function related to STOP
# ******************************************************************


def _stopLocal(protocol):
    if protocol.useQueue() and not protocol.isScheduled():
        jobId = protocol.getJobId()
        host = protocol.getHostConfig()
        cancelCmd = host.getCancelCommand() % {'JOB_ID': jobId}
        _run(cancelCmd, wait=True)
    else:
        process.killWithChilds(protocol.getPid())


def _stopRemote(protocol):
    _runRemote(protocol, 'stop')
