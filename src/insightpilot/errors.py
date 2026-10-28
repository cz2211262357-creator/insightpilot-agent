class DomainError(Exception):
    def __init__(self, code, status=409, message=None):
        self.code = code
        self.status = status
        self.message = message or code.replace("_", " ")
        super().__init__(self.message)
