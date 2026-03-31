"""通用小工具：YAML 加载、重试装饰器、时间字符串与结果目录归档。"""

import datetime
import logging
import os
import shutil

import yaml


def load_from_file(filename: str):
    """从文件读取 YAML（可能多文档），返回文档列表。"""
    with open(filename, 'r') as file:
        workload = list(yaml.safe_load_all(file))
        logging.info(f'Workload {filename} loaded, with {len(workload)} jobs')
        return workload


def do_until_no_error(func):
    """装饰器：被装饰函数异常时记录日志并无限重试直至成功。"""

    def wrapper(*args, **kwargs):
        while True:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logging.exception(e)
    return wrapper


def now_str():
    """当前时间，格式 ``年-月-日-时-分-秒``，用于目录名等。"""
    return datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')


def now_str_millisecond():
    """当前时间，含微秒后缀。"""
    return datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S.%f')


def makeup_results_dir():
    """若已存在 ``results``，则移入 ``old-results/<时间戳>`` 并重新创建空 ``results``。"""
    results_dir_exists = os.path.exists('results')
    os.makedirs('results', exist_ok=True)
    if results_dir_exists:
        os.makedirs('old-results', exist_ok=True)
        shutil.move('results', 'old-results')
        os.rename('old-results/results', f'old-results/{now_str()}')
        os.makedirs('results', exist_ok=True)
