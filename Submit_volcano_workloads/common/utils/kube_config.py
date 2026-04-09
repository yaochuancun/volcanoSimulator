"""Load Kubernetes client config from a local kubeconfig file (for tools that talk to a real cluster)."""

import kubernetes

from .. import consts


def load_kube_config(filename: str = consts.KUBE_CONFIG_FILENAME):
    """Load kubeconfig from the given path; default filename comes from package constants."""
    kubernetes.config.kube_config.load_kube_config(config_file=filename)
