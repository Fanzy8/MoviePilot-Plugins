import time

import requests
from requests.adapters import HTTPAdapter

from .config import Config

# 线程级共享的Session（连接池跨任务复用，高延迟下省掉重复 TCP/TLS 握手）
_SESSIONS = {}


def _get_session(pool_size: int) -> requests.Session:
    """按连接池大小缓存 Session。
    pool_size<=0 表示复用默认池(10)，>=1 时并发池按需扩容。
    """
    size = pool_size if pool_size and pool_size > 0 else 10
    keysize = size
    sess = _SESSIONS.get(keysize)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=size,
            pool_maxsize=size,
            max_retries=0,          # 重试由本模块统一控制，避免与业务重试叠加
            pool_block=True,
        )
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _SESSIONS[keysize] = sess
    return sess


def _split_timeout(timeout):
    """把timeout 归一化成 requests 接受的 (connect, read) 元组。

    - 元组/列表：原样使用（调用方可能已指定分阶段超时）
    - None：用 Config.CONNECT_TIMEOUT / Config.REQUEST_TIMEOUT 兜底
    - 数字或字符串：视为读取超时，连接超时取 Config.CONNECT_TIMEOUT
    """
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        return tuple(timeout)
    if timeout is None:
        return (Config.CONNECT_TIMEOUT, Config.REQUEST_TIMEOUT)
    try:
        read = float(timeout)
    except (TypeError, ValueError):
        read = float(Config.REQUEST_TIMEOUT)
    return (Config.CONNECT_TIMEOUT, read)


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否值得重试。
    仅对连接/读取类瞬时故障重试；HTTP 4xx/5xx 由业务判断，不在此重试。
    """
    return isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout,
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError))


def _request(method, url, headers=None, params=None, data=None, json=None,
             timeout=Config.REQUEST_TIMEOUT, retries=None, retry_backoff=None,
             pool_size=None):
    sess = _get_session(pool_size)
    tmo = _split_timeout(timeout)

    if retries is None:
        retries = Config.RETRY_TIMES
    if retry_backoff is None:
        retry_backoff = Config.RETRY_BACKOFF

    last_exc = None
    for attempt in range(1, max(1, int(retries) + 1) + 1):
        try:
            resp = sess.request(method, url, headers=headers, params=params,
                                data=data, json=json, timeout=tmo)
            # 5xx 视为瞬时故障可重试；4xx 直接返回交由业务处理
            if resp.status_code >= 500 and attempt <= retries:
                last_exc = requests.exceptions.HTTPError(
                    f"HTTP {resp.status_code}")
                time.sleep(retry_backoff * attempt)
                continue
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt > retries or not _is_retryable(exc):
                raise
            time.sleep(retry_backoff * attempt)
    # 理论上不会走到这里（要么 return要么 raise）
    raise last_exc if last_exc else requests.exceptions.RequestException("请求失败")


class CustomRequests:
    @staticmethod
    def get(url, headers=None, params=None, timeout=Config.REQUEST_TIMEOUT,
            retries=None, retry_backoff=None, pool_size=None):
        return _request("GET", url, headers=headers, params=params, timeout=timeout,
                        retries=retries, retry_backoff=retry_backoff, pool_size=pool_size)

    @staticmethod
    def post(url, headers=None, data=None, timeout=Config.REQUEST_TIMEOUT,
             retries=None, retry_backoff=None, pool_size=None):
        return _request("POST", url, headers=headers, data=data, timeout=timeout,
                        retries=retries, retry_backoff=retry_backoff, pool_size=pool_size)

    @staticmethod
    def put(url, headers=None, data=None, timeout=Config.REQUEST_TIMEOUT,
            retries=None, retry_backoff=None, pool_size=None):
        return _request("PUT", url, headers=headers, data=data, timeout=timeout,
                        retries=retries, retry_backoff=retry_backoff, pool_size=pool_size)

    @staticmethod
    def delete(url, headers=None, data=None, timeout=Config.REQUEST_TIMEOUT,
               retries=None, retry_backoff=None, pool_size=None):
        return _request("DELETE", url, headers=headers, data=data, timeout=timeout,
                        retries=retries, retry_backoff=retry_backoff, pool_size=pool_size)