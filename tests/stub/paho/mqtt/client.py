"""最小的 paho.mqtt 桩，仅用于本地端到端冒烟测试（不连真 broker）。"""


class CallbackAPIVersion:
    VERSION1 = 1
    VERSION2 = 2


class MQTTv311:
    pass


class Client:
    def __init__(self, *args, **kwargs):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.subscriptions = []

    def username_pw_set(self, username, password=None):
        pass

    def tls_set(self, *args, **kwargs):
        pass

    def tls_insecure_set(self, value):
        pass

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass

    def connect(self, host, port, keepalive=60):
        raise OSError(f"stub client: {host}:{port} unreachable")

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topics, qos=0):
        self.subscriptions.extend(topics)
        return (0, 1)
