"""A single reusable HTTP connection for the ordered latest-frame outbox."""

import http.client
import json
import urllib.error
from urllib.parse import urlsplit


class StreamHttpSender:
    def __init__(self, url, token, timeout=0.3):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError('Invalid stream endpoint')
        self.host, self.port = parsed.hostname, parsed.port
        self.path = parsed.path.rstrip('/') + '/stream'
        self.factory = (http.client.HTTPSConnection if parsed.scheme == 'https'
                        else http.client.HTTPConnection)
        self.url, self.token, self.timeout = url, token, timeout
        self.connection = None
        self.connections = 0

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def post(self, message):
        if self.connection is None:
            self.connection = self.factory(self.host, self.port, timeout=self.timeout)
        if self.connection.sock is None:
            self.connections += 1
        try:
            self.connection.request('POST', self.path, json.dumps(message).encode(), {
                'Content-Type': 'application/json', 'X-Adapter-Token': self.token,
                'Connection': 'keep-alive',
            })
            response = self.connection.getresponse()
            body = response.read(4097)
            if response.status != 200:
                raise urllib.error.HTTPError(
                    self.url, response.status, 'Stream request rejected', {}, None)
            if len(body) > 4096:
                raise ValueError('Oversized stream confirmation')
            result = json.loads(body)
            if not isinstance(result, dict) or result.get('accepted') is not True:
                raise ValueError('Frame rejected by host')
        except (OSError, ValueError, http.client.HTTPException):
            self.close()
            raise
