"""从本地 kubeconfig 文件加载 Kubernetes 客户端配置（供连接真实集群的工具使用）。"""

import kubernetes

from .. import consts


def load_kube_config(filename: str = consts.KUBE_CONFIG_FILENAME):
    """加载指定路径的 kubeconfig，默认使用包内常量中的文件名。"""
    kubernetes.config.kube_config.load_kube_config(config_file=filename)
