class Config:
    # 读取超时：等待服务器响应数据的最大秒数
    REQUEST_TIMEOUT = 30  # Default timeout for requests in seconds

    # 连接超时：建立 TCP/TLS 连接的最大秒数。
    # 与读取超时分开配置，慢站时连接阶段能快速失败，不必白等读取预算。
    CONNECT_TIMEOUT = 10

    # 瞬时故障(连接/读取超时、连接重置)重试次数与退避基数(秒)
    RETRY_TIMES = 2
    RETRY_BACKOFF = 1.0