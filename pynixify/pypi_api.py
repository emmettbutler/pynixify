# pynixify - Nix expression generator for Python packages
# Copyright (C) 2020 Matías Lang

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import asyncio
import hashlib
import json
import os
import sys
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import quote, urlparse, urlunparse

import aiofiles
import aiohttp
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version, parse

from pynixify.base import Package, parse_version
from pynixify.exceptions import IntegrityError


class ABCPyPICache(metaclass=ABCMeta):
    @abstractmethod
    async def fetch(self, package_name: str) -> object:
        pass

    @abstractmethod
    async def fetch_url(self, url: str, sha256: str) -> Path:
        pass


@dataclass
class PyPIPackage(Package):
    sha256: str
    download_url: str
    pypi_name: str
    pypi_cache: ABCPyPICache
    local_source: Optional[Path] = None

    async def source(self, extra_args=[]) -> Path:
        if self.local_source is not None:
            return self.local_source
        downloaded_file: Path = await self.pypi_cache.fetch_url(
            self.download_url, self.sha256
        )
        h = hashlib.sha256()
        with downloaded_file.open("rb") as fp:
            while True:
                data = fp.read(65536)
                if not data:
                    break
                h.update(data)
        if h.hexdigest() != self.sha256:
            raise IntegrityError(
                f"SHA256 hash does not match. The hash of {self.download_url} "
                f"should be {self.sha256} but it is {h.hexdigest()} instead."
            )
        self._cached_downloaded_file = downloaded_file
        return downloaded_file

    @property
    def filename(self):
        return Path(urlparse(self.download_url).path).name

    @property
    def attr(self):
        return self.pypi_name

    def __str__(self):
        return f"PyPIPackage(attr={self.attr}, version={self.version})"


class PyPIData:
    def __init__(self, pypi_cache):
        self.pypi_cache = pypi_cache

    async def from_requirement(self, req: Requirement) -> Sequence[PyPIPackage]:
        response = await self.pypi_cache.fetch(canonicalize_name(req.name))
        matching = []
        for version, version_dist in response["releases"].items():
            try:
                data = next(e for e in version_dist if e["packagetype"] == "sdist")
            except StopIteration:
                continue
            if version in req.specifier:
                matching.append(
                    PyPIPackage(
                        sha256=data["digests"]["sha256"],
                        version=parse_version(version),
                        download_url=data["url"],
                        pypi_name=canonicalize_name(req.name),
                        pypi_cache=self.pypi_cache,
                    )
                )
        return matching


class PyPICache:
    async def fetch(self, package_name):
        url = f"https://pypi.org/pypi/{quote(package_name)}/json"
        async with aiohttp.ClientSession(raise_for_status=True) as session:
            async with session.get(url) as response:
                return await response.json()

    async def fetch_url(self, url, sha256) -> Path:
        from pynixify.expression_builder import escape_string

        expr = f"""
            builtins.fetchurl {{
                url = {escape_string(url)};
                sha256 = {escape_string(sha256)};
            }}
        """
        result = await nix_instantiate(expr)
        assert isinstance(result, str)
        return Path(result)


async def nix_instantiate(expr: str, attr=None, **kwargs):
    extra_args: List[str] = []
    if attr is not None:
        extra_args += ["--attr", attr]
    for k, v in kwargs.items():
        extra_args += ["--arg", k, v]

    proc = await asyncio.create_subprocess_exec(
        "nix-instantiate",
        "--json",
        "--eval",
        "-",
        *extra_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    proc.stdin.write(expr.encode())  # type: ignore
    proc.stdin.write_eof()  # type: ignore
    stdout, stderr = await proc.communicate()
    status = await proc.wait()
    assert (await proc.wait()) == 0
    return json.loads(stdout.decode())


async def get_path_hash(path: Path) -> str:
    url = urlunparse(
        (
            "file",
            "",
            str(path.resolve()),
            "",
            "",
            "",
        )
    )
    proc = await asyncio.create_subprocess_exec(
        "nix-prefetch-url",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    (stdout, stderr) = await proc.communicate()
    status = await proc.wait()
    if status:
        print(stderr.decode(), file=sys.stderr)
        raise RuntimeError(f"nix-prefetch-url failed with code {status}")
    return stdout.decode().strip()
