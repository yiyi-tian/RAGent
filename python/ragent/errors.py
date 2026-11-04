"""可安全返回客户端的模型错误。"""


class ModelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
