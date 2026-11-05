import logging
import re


class RedactRequestQuery(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(
            r'(\b(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)\s+[^?\s"]+)\?[^"\s]*',
            r"\1",
            record.getMessage(),
        )
        record.args = ()
        return True
