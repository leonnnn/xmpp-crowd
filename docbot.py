#!/usr/bin/python3
from hub import HubBot
import traceback
import itertools
import sys, os
import select
import threading
import subprocess
import tempfile

class Popen(subprocess.Popen):
    @classmethod
    def checked(cls, call, *args, **kwargs):
        proc = cls(call, *args, **kwargs)
        result = proc.communicate()
        retval = proc.wait()
        if retval != 0:
            raise subprocess.CalledProcessError(retval, " ".join(call))
        return result

    def __init__(self, call, *args, sink_line_call=None, **kwargs):
        if sink_line_call is not None:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        super().__init__(call, *args, **kwargs)
        self.sink_line_call = sink_line_call
        if sink_line_call is not None:
            sink_line_call("$ {cmd}".format(cmd=" ".join(call)).encode())

    def _submit_buffer(self, buf, force=False):
        if b"\n" not in buf:
            if force:
                self.sink_line_call(buf)
                return b""
            else:
                return buf

        split = buf.split(b"\n")
        for line in split[:-1]:
            self.sink_line_call(buf)
        return split[-1]

    def communicate(self):
        if self.sink_line_call is not None:
            buffers = [b"", b""]
            rlist = [self.stdout, self.stderr]
            while True:
                rs, _, _ = select.select(rlist, [], [])
                for i, fd in enumerate(reversed(rs)):
                    read = fd.readline()
                    if len(read) == 0:
                        del rlist[len(rs)-(i+1)]
                        buf = buffers[len(rs)-(i+1)]
                        if len(buf):
                            self._submit_buffer(buf, True)
                        del buffers[len(rs)-(i+1)]
                        continue
                    buffers[i] += read
                    buffers[i] = self._submit_buffer(buffers[i])
                if len(rlist) == 0:
                    break
            for buf in buffers:
                self._submit_buffer(buf, True)
            return None, None
        else:
            return super().communicate()

class Build:
    def __init__(self, name, *args,
            branch="master",
            submodules=[],
            commands=["make"],
            working_copy=None,
            **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name
        self.branch = branch
        self.submodules = submodules
        self.commands = commands
        self.working_copy = working_copy

    def build_environment(self, log_func):
        return self.project.build_environment(
            log_func,
            self.branch,
            self.submodules,
            working_copy=self.working_copy
        )

    def _do_build(self, env):
        def checked(*args, **kwargs):
            return Popen.checked(*args, sink_line_call=env.log_func, **kwargs)

        for command in self.commands:
            checked(command)

    def build(self, log_func):
        with self.build_environment(log_func) as env:
            self._do_build(env)

class BuildAndMove(Build):
    def __init__(self, *args, move_to=None, move_from=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not move_to:
            raise ValueError("Required parameter move_to missing or empty.")
        self.move_to = move_to
        self.move_from = move_from

    def _do_build(self, env):
        def checked(*args, **kwargs):
            return Popen.checked(*args, sink_line_call=env.log_func, **kwargs)

        super()._do_build(env)
        if self.move_from is not None:
            move_from = self.move_from.format(
                builddir=env.tmp_dir
            )
        else:
            move_from = env.tmp_dir
        checked(["rm", "-rf", self.move_to])
        checked(["mv", move_from, self.move_to])


class BuildEnvironment:
    def __init__(self, tmp_dir, repo_url, branch, submodules, log_func):
        self.tmp_dir_context = None
        self.tmp_dir = tmp_dir
        self.repo_url = repo_url
        self.branch = branch
        self.submodules = submodules
        self.log_func = log_func

    def __enter__(self):
        def checked(*args, **kwargs):
            return Popen.checked(*args, sink_line_call=self.log_func, **kwargs)

        if self.tmp_dir is None:
            self.tmp_dir_context = tempfile.TemporaryDirectory()
            self.tmp_dir = self.tmp_dir_context.name
        try:
            if not os.path.isdir(self.tmp_dir):
                os.makedirs(self.tmp_dir)
            os.chdir(self.tmp_dir)
            if os.path.isdir(os.path.join(self.tmp_dir, ".git")):
                checked(["git", "fetch", "origin"])
            else:
                checked(["git", "clone", self.repo_url, self.tmp_dir])

            checked(["git", "checkout", self.branch])
            checked(["git", "pull"])

            for submodule in self.submodules:
                checked(["git", "submodule", "init", submodule])
                checked(["git", "submodule", "update", submodule])
        except:
            if self.tmp_dir_context is not None:
                self.tmp_dir_context.cleanup()
            self.tmp_dir_context = None
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.tmp_dir_context is not None:
            self.tmp_dir_context.cleanup()
        return False


class Project:
    @classmethod
    def declare(cls, name, *args, **kwargs):
        return (name, cls(name, *args, **kwargs))

    def __init__(self, name, *builds,
            repository_url=None, pubsub_name=None, working_copy=None,
            **kwargs):
        super().__init__(**kwargs)
        if not repository_url:
            raise ValueError("Required parameter repository_url missing or empty.")

        self.name = name
        self.repository_url = repository_url
        self.pubsub_name = pubsub_name
        self.working_copy = working_copy
        self.builds = builds
        for build in self.builds:
            build.project = self

        if pubsub_name is not None:
            triggers = {}
            for build in self.builds:
                build_list = triggers.setdefault((self.pubsub_name, build.branch), [])
                build_list.append(build)
            self.triggers = triggers
        else:
            self.triggers = {}

    def build_environment(self, log_func, branch, submodules,
            working_copy=None):
        return BuildEnvironment(
            working_copy or self.working_copy,
            self.repository_url,
            branch,
            submodules,
            log_func
        )

class DocBot(HubBot):
    LOCALPART = "docbot"
    NICK = "buildbot"
    PASSWORD = ""
    GIT_NODE = "git@"+HubBot.FEED

    def __init__(self):
        super(DocBot, self).__init__(self.LOCALPART, "core", self.PASSWORD)
        self.switch, self.nick = self.addSwitch("docs", "buildbot", self.docsSwitch)
        self.bots_switch, _ = self.addSwitch("bots", "buildbot")
        error = self.reloadConfig()
        if error:
            traceback.print_exception(*error)
            sys.exit(1)

        self.add_event_handler("pubsub_publish", self.pubsubPublish)

    def sessionStart(self, event):
        super(DocBot, self).sessionStart(event)
        iq = self.pubsub.get_subscriptions(self.FEED, self.GIT_NODE)
        if len(iq["pubsub"]["subscriptions"]) == 0:
            self.pubsub.subscribe(self.FEED, self.GIT_NODE, bare=True)
        self.send_message(mto=self.switch, mbody="", msubject="idle", mtype="groupchat")

    def reloadConfig(self):
        namespace = {}
        f = open("docbot_config.py", "r")
        conf = f.read()
        f.close()
        try:
            exec(conf, globals(), namespace)
        except Exception:
            return sys.exc_info()
        self.authorized = set(namespace.get("authorized", []))
        self.blacklist = set()
        self.projects = dict(namespace.get("projects", []))

        self.repobranch_map = {}
        for project in self.projects.values():
            for reprobranch, build in project.triggers.items():
                self.repobranch_map.setdefault(reprobranch, []).extend(build)
        print(self.repobranch_map)
        return None

    def docsSwitch(self, msg):
        pass

    def pubsubPublish(self, msg):
        item = msg["pubsub_event"]["items"]["item"].xml[0]
        repo = item.findtext("{http://hub.sotecware.net/xmpp/git-post-update}repository")
        if repo is None:
            print("Malformed git-post-update.")
        ref = item.findtext("{http://hub.sotecware.net/xmpp/git-post-update}ref")
        if ref is None:
            print("Malformed git-post-update.")

        repobranch = (repo, ref.split("/")[2])
        try:
            builds = self.repobranch_map[repobranch]
        except KeyError:
            print(repobranch)
            return
        for build in builds:
            self.rebuild(build)

    def formatException(self, exc_info):
        return "\n".join(traceback.format_exception(*sys.exc_info()))

    def replyException(self, msg, exc_info):
        self.reply(msg, self.formatException(exc_info))

    def authorizedSource(self, msg):
        origin = str(msg["from"].bare)
        if not origin in self.authorized:
            if not origin in self.blacklist:
                self.reply(msg, "You're not authorized.")
                self.blacklist.add(origin)
            return

    def messageMUC(self, msg):
        if msg["mucnick"] == self.nick:
            return
        contents = msg["body"].strip()
        if contents == "ping":
            self.reply(msg, "pong")
            return
        #if not self.authorizedSource(msg):
        #    return

    def message(self, msg):
        if msg["type"] == "groupchat":
            return

        contents = msg["body"]
        args = contents.split(" ")
        cmd = args[0]
        args = args[1:]
        handler = self.COMMANDS.get(cmd, None)
        if handler is not None:
            try:
                local = {"__func": handler, "__self": self, "__msg": msg}
                self.reply(msg, repr(eval("__func(__self, __msg, {0})".format(", ".join(args)), globals(), local)))
            except Exception:
                self.replyException(msg, sys.exc_info())
        else:
            self.reply(msg, "Unknown command: {0}".format(cmd))

    def rebuild(self, build):
        def log_func(msg):
            self.send_message(
                mto=self.switch,
                mbody=msg,
                mtype="groupchat"
            )
        def log_func_binary(buf):
            msg = buf.decode().strip()
            if msg:
                log_func(msg)
        project = build.project

        topic = "Rebuilding {0} from project {1}".format(build.name, build.project.name)
        self.send_message(mto=self.switch, mbody="", msubject=topic, mtype="groupchat")
        try:
            log_func(topic)
            build.build(log_func_binary)
            log_func("done.")
        except Exception as err:
            self.send_message(
                mto=self.bots_switch,
                mbody="jonas: Project {0}, target {1} is broken, traceback logged to docs".format(project.name, build.name),
                mtype="groupchat"
            )
            self.send_message(
                mto=self.switch,
                mbody=self.formatException(err),
                mtype="groupchat"
            )
            print("Exception during docbuild logged to muc.")
        finally:
            self.send_message(
                mto=self.switch,
                mbody="",
                msubject="docbot is idle",
                mtype="groupchat"
            )

    def cmdRebuild(self, msg, projectName):
        project = self.projects.get(projectName, None)
        if not project:
            return "Unknown project: {0}".format(projectName)
        self.rebuild(project)
        return True

    def cmdReload(self, msg):
        result = self.reloadConfig()
        if result:
            self.replyException(msg, result)
        else:
            return True

    def cmdEcho(self, msg, *args):
        return " ".join((str(arg) for arg in args))

    COMMANDS = {
        "rebuild": cmdRebuild,
        "reload": cmdReload,
        "echo": cmdEcho
    }

if __name__=="__main__":
    docbot = DocBot()
    docbot.run()

