# freebind-py

Use a routed IPv6 allocation as a pool of source addresses for Python sockets,
Requests, aiohttp, HTTPX, or curl_cffi. The core uses only the standard library;
socket operations require Linux and Python 3.11 or newer.

```sh
python -m pip install freebind-py
```

```python
import socket
from freebind import Source, new_socket

with new_socket(Source("::1"), type=socket.SOCK_DGRAM) as sock:
    print(sock.getsockname())
```

Replace `::1` with an address your network routes to this machine. Freebind
permits binding nonlocal source addresses; it does not configure routing. See
[the user guide](README.md) for prefixes, HTTP integrations, and troubleshooting.

```{toctree}
:maxdepth: 2
:hidden:

README
api
CONTRIBUTING
```

## Project links

- [Source code](https://github.com/minipps/freebind-py)
- [Changelog](https://github.com/minipps/freebind-py/blob/main/CHANGELOG.md)
- [Issue tracker](https://github.com/minipps/freebind-py/issues)
- [License (GPL-3.0-only)](https://github.com/minipps/freebind-py/blob/main/LICENSE)
