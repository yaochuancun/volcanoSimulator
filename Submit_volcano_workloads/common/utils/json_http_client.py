"""简易 JSON HTTP 客户端：对仿真器等服务发起请求并解析 JSON 响应，支持失败重试。"""

import json
import logging
import time

import requests


class JsonHttpClient(object):
    """封装 base URL，提供带重试的 JSON 请求。"""

    def __init__(self, host: str):
        self.host = host

    def get_json(self, path: str, retry: int = -1, method: str = 'GET', **kwargs) -> dict:
        """请求 ``path``，返回解析后的 dict。retry=-1 表示无限重试直至成功。"""
        url = join_url(self.host, path)
        retry_times = 0
        while retry_times != retry:
            try:
                response = requests.request(method, url, **kwargs)
                response.raise_for_status()
                if retry_times:
                    logging.warning(f'Succeed after retry {retry_times} times, URL: {url}')
                return json.loads(response.text)
            except Exception as e:
                logging.debug(e)
                retry_times += 1
                if retry_times % 100 == 0:
                    logging.error(e)
                time.sleep(0.1)


def join_url(lhs, rhs):
    """拼接主机与路径，保证路径以 ``/`` 开头。"""
    if len(rhs) and rhs[0] != '/':
        rhs = '/' + rhs
    return lhs + rhs
