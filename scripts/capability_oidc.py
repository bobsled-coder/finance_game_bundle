#!/usr/bin/env python3
"""Small HTTPS JWKS endpoint for the disposable capability gate only."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import ssl


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/.well-known/jwks.json":
            self.send_error(404)
            return
        body = open("/fixture/jwks.json", "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


server = HTTPServer(("0.0.0.0", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/fixture/tls.crt", "/fixture/tls.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
