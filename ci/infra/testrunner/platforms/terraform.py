import hcl
import json
import os
import subprocess

from timeout_decorator import timeout

from utils import (Format, step, Utils)


class Terraform:
    def __init__(self, conf, platform):
        self.conf = conf
        self.utils = Utils(conf)
        self.tfdir = os.path.join(self.conf.terraform.tfdir, platform)
        self.tfjson_path = os.path.join(conf.workspace, "tfout.json")
        self.state = None

    def _env_setup_cmd(self):
        """Returns the command for setting up the platform environment"""
        return ""

    def _cleanup_platform(self):
        """Platform specific cleanup. Expected to be overridden by platforms"""

    def _get_platform_logs(self):
        """Platform specific logs to collect. Expected to be overridden by platforms"""
        return False

    def cleanup(self):
        """ Clean up """
        cleanup_failure = False
        try:
            self._cleanup_platform()
        except Exception as ex:
            cleanup_failure = True
            print(Format.alert("Received the following error {}".format(ex)))
            print("Attempting to finish cleanup")

        dirs = [os.path.join(self.conf.workspace, "tfout"),
                self.tfjson_path]

        for tmp_dir in dirs:
            try:
                self.utils.runshellcommand("rm -rf {}".format(tmp_dir))
            except Exception as ex:
                cleanup_failure = True
                print("Received the following error {}".format(ex))
                print("Attempting to finish cleanup")

        if cleanup_failure:
            raise Exception(Format.alert("Failure(s) during cleanup"))

    @timeout(600)
    @step
    def gather_logs(self):
        logging_errors = False

        node_ips = {"master": self.get_nodes_ipaddrs("master"),
                    "worker": self.get_nodes_ipaddrs("worker")}
        logs = {"files": ["/var/run/cloud-init/status.json",
                          "/var/log/cloud-init-output.log",
                          "/var/log/cloud-init.log"],
                "dirs": ["/var/log/pods"],
                "services": ["kubelet"]}

        if not os.path.isdir(self.conf.log_dir):
            os.mkdir(self.conf.log_dir)
            print(f"Created log dir {self.conf.log_dir}")

        for node_type in node_ips:
            for ip_address in node_ips[node_type]:
                node_log_dir = self._create_node_log_dir(ip_address, node_type, self.conf.log_dir)
                logging_error = self.utils.collect_remote_logs(ip_address, logs, node_log_dir)

                if logging_error:
                    logging_errors = logging_error

        platform_log_error = self._get_platform_logs()

        if platform_log_error:
            logging_errors = platform_log_error

        return logging_errors

    @step
    def provision(self, num_master=-1, num_worker=-1):
        """ Create and apply terraform plan"""
        if num_master > -1 or num_worker > -1:
            print("Overriding number of nodes")
            if num_master > -1:
                self.conf.master.count = num_master
                print("   Masters:{} ".format(num_master))

            if num_worker > -1:
                self.conf.worker.count = num_worker
                print("   Workers:{} ".format(num_worker))

        print("Init terraform")
        self._check_tf_deployed()
        
        self.utils.setup_ssh()

        init_cmd = "terraform init"
        if self.conf.terraform.plugin_dir:
            print("Installing plugins from {}".format(self.conf.terraform.plugin_dir))
            init_cmd = init_cmd+" -plugin-dir="+self.conf.terraform.plugin_dir
        self._runshellcommandterraform(init_cmd)

        self._runshellcommandterraform("terraform version")
        self._generate_tfvars_file()
        plan_cmd = ("{env_setup};"
                    " terraform plan "
                    " -out {workspace}/tfout".format(
                        env_setup=self._env_setup_cmd(),
                        workspace=self.conf.workspace))
        apply_cmd = ("{env_setup};"
                     "terraform apply -auto-approve {workspace}/tfout".format(
                        env_setup=self._env_setup_cmd(),
                        workspace=self.conf.workspace))

        # TODO: define the number of retries as a configuration parameter
        for retry in range(1, 5):
            print(Format.alert("Run terraform plan - execution # {}".format(retry)))
            self._runshellcommandterraform(plan_cmd)
            print(Format.alert("Run terraform apply - execution # {}".format(retry)))
            try:
                self._runshellcommandterraform(apply_cmd)
                break

            except Exception:
                print("Failed terraform apply n. %d" % retry)
                if retry == 4:
                    print(Format.alert("Failed Openstack Terraform deployment"))
                    raise
            finally:
                self._fetch_terraform_output()

    @staticmethod
    def _create_node_log_dir(ip_address, node_type, log_dir_path):
        node_log_dir_path = os.path.join(log_dir_path, f"{node_type}_{ip_address.replace('.', '_')}")

        if not os.path.isdir(node_log_dir_path):
            os.mkdir(node_log_dir_path)
            print(f"Created log dir {node_log_dir_path}")

        return node_log_dir_path

    def _load_tfstate(self):
        if self.state is None:
            fn = os.path.join(self.tfdir, "terraform.tfstate")
            print("Reading {}".format(fn))
            with open(fn) as f:
                self.state = json.load(f)

    def get_lb_ipaddr(self):
        self._load_tfstate()
        return self.state["modules"][0]["outputs"]["ip_load_balancer"]["value"]

    def get_nodes_ipaddrs(self, role):
        self._load_tfstate()

        if role not in ("master", "worker"):
            raise ValueError("Invalid role: {}".format(role))

        role_key = "ip_"+role+"s"
        return self.state["modules"][0]["outputs"][role_key]["value"]

    @step
    def _fetch_terraform_output(self):
        cmd = ("{env_setup};"
               "terraform output -json >"
               "{json_f}".format(
                   env_setup=self._env_setup_cmd(),
                   json_f=self.tfjson_path))
        self._runshellcommandterraform(cmd)

    def _generate_tfvars_file(self):
        """Generate terraform tfvars file"""
        tfvars_template = os.path.join(self.tfdir, self.conf.terraform.tfvars)
        tfvars_final = os.path.join(self.tfdir, "terraform.tfvars.json")

        with open(tfvars_template) as f:
            if '.json' in os.path.basename(tfvars_template).lower():
                tfvars = json.load(f)
            else:
                tfvars = hcl.load(f)
                
            self._update_tfvars(tfvars)

            with open(tfvars_final, "w") as f:
                json.dump(tfvars, f)

    def _update_tfvars(self, tfvars):
        new_vars = {
            "internal_net": self.conf.terraform.internal_net,
            "stack_name": self.conf.terraform.stack_name,
            "username": self.conf.nodeuser,
            "masters": self.conf.master.count,
            "workers": self.conf.worker.count,
            "authorized_keys": [self.utils.authorized_keys()]
        }

        for k, v in new_vars.items():
            if tfvars.get(k) is not None:
                if isinstance(v, list):
                    tfvars[k] = tfvars[k] + v
                elif isinstance(v, dict):
                    tfvars[k].update(v)
                else:
                    tfvars[k] = v

        # Update mirror urls
        repos = tfvars.get("repositories")
        if self.conf.terraform.mirror and repos is not None:
            for name, url in repos.items():
                tfvars["repositories"][name] = url.replace("download.suse.de", self.conf.terraform.mirror)

    def _runshellcommandterraform(self, cmd, env={}):
        """Running terraform command in {terraform.tfdir}/{platform}"""
        cwd = self.tfdir

        # Terraform needs PATH and SSH_AUTH_SOCK
        sock_fn = self.utils.ssh_sock_fn()
        env["SSH_AUTH_SOCK"] = sock_fn
        env["PATH"] = os.environ['PATH']

        print(Format.alert("$ {} > {}".format(cwd, cmd)))
        subprocess.check_call(cmd, cwd=cwd, shell=True, env=env)

    def _check_tf_deployed(self):
        if os.path.exists(self.tfjson_path):
            raise Exception(Format.alert("tf file found. Please run cleanup and try again{}"))

    # TODO: this function is currently not used. Identify points where it should
    # be invoked
    def _verify_tf_dependency(self):
        if not os.path.exists(self.tfjson_path):
            raise Exception(Format.alert("tf file not found. Please run terraform and try again{}"))
