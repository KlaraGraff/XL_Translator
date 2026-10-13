"""「文字识别组件」（Tesseract OCR）的一键安装。

程序和它用到的库取自 conda-forge 编译好的包（固定版本，整包核对 SHA-256），只拿
tesseract 实际加载的那些文件；tesseract 包本身约 180 MB（里面打着一百多种语言），只按
Range 读开头一两兆取出程序，逐个文件核对。语言数据单独从 tessdata_fast 4.1.0 下载
（conda-forge 包里的就是这一版，逐字节相同），只装 ``core.pdf_vector`` 会用到的几种。

装好的目录（``plugins/ocr-component/<MANIFEST_VERSION>-<随机>/``）：
``bin/tesseract``、``lib/``（macOS、Linux 的库；Windows 的 DLL 和程序都在 ``bin/``）、
``tessdata/``。用时把 TESSDATA_PREFIX 指到 ``tessdata/``（``core.ocr_engine``）。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from loguru import logger

from core.app_paths import get_app_data_dir
from core.component_download import (
    ComponentDownloadError,
    RemoteFile,
    conda_file,
    download_files,
    extract_conda,
    stream_conda_files,
)

MANIFEST_VERSION = "1"
TESSERACT_VERSION = "5.5.3"


@dataclass(frozen=True)
class CondaPackage:
    name: str
    size: int
    sha256: str
    files: tuple[str, ...]  # 包内路径；「路径>新名字」表示装好后换个名字（版本号短一截的那个）


class TesseractStream:
    def __init__(self, name: str, data_offset: int, data_size: int, *files: tuple[str, int, str]) -> None:
        self.name = name
        self.data_offset = data_offset
        self.data_size = data_size
        self.files = files


@dataclass(frozen=True)
class OcrManifest:
    subdir: str
    tesseract: TesseractStream
    packages: tuple[CondaPackage, ...]


def _conda(name: str, size: int, sha256: str, *files: str) -> CondaPackage:
    return CondaPackage(name, size, sha256, files)


_TESSDATA_HOSTS = (
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/",
    "https://cdn.jsdelivr.net/gh/tesseract-ocr/tessdata_fast@4.1.0/",
)


def _tessdata(lang: str, size: int, sha256: str) -> RemoteFile:
    name = f"{lang}.traineddata"
    return RemoteFile(name, tuple(host + name for host in _TESSDATA_HOSTS), size, sha256)


# tesseract 的配置文件只用到 tsv 一个（core.pdf_vector.extract 按 TSV 读结果），内容固定。
_CONFIGS = {"tsv": "tessedit_create_tsv 1\n"}

# 由 conda 的依赖解析（tesseract=5.5.3，macOS 不低于 12）得出，再按程序实际加载的库
# （Mach-O / PE / ELF 的依赖表）筛掉用不到的包。升级时整份重新生成，MANIFEST_VERSION 加一。
MANIFEST: dict[str, OcrManifest] = {
    "macos-arm64": OcrManifest(
        subdir="osx-arm64",
        tesseract=TesseractStream(
            "tesseract-5.5.3-h32d8946_0.conda", 181, 180142716,
            ("bin/tesseract", 117808, "db942d7d648135b778b048370f628a10713d6c2d7eec6eb9b423593dc1abab7a"),
            ("lib/libtesseract.5.dylib", 3176960, "6824a424779d2aa806c97feafe668e629e990f78b3d9860c36a672aef4209e2d"),
        ),
        packages=(
            _conda("bzip2-1.0.8-h4e30115_10.conda", 124965, "8ec22f0ba25cbfc2e64d70cf29459eccd7ffdf6436f6a6ff15bbfef799f7d4f6",
                   "lib/libbz2.1.0.8.dylib>libbz2.dylib"),
            _conda("giflib-5.2.2-hd20048c_1.conda", 73137, "be9b4cf253c7d3bf20ccc9ff278ea7ec6f144ebbe8fdc9a131d18c35ff49692f",
                   "lib/libgif.7.2.0.dylib>libgif.7.dylib"),
            _conda("icu-78.3-py310h579977c_2.conda", 14070242, "6cdb5dee54c72e56ab189fb3ad33cb28533553d42590e7e831160248f4416a43",
                   "lib/libicudata.78.3.dylib>libicudata.78.dylib", "lib/libicuuc.78.3.dylib>libicuuc.78.dylib"),
            _conda("krb5-1.22.2-h34f8a20_2.conda", 1165740, "aaea1d42b07769920db2af7471ece9399d2613448863da64ad8272998db5db34",
                   "lib/libcom_err.3.0.dylib", "lib/libgssapi_krb5.2.2.dylib", "lib/libk5crypto.3.1.dylib", "lib/libkrb5.3.3.dylib", "lib/libkrb5support.1.1.dylib"),
            _conda("leptonica-1.87.0-hbd60906_1.conda", 2203465, "1f9c620c37f04e5997f38bc0caef5c835bd5f1e07631aef196068d628879fbea",
                   "lib/libleptonica.6.dylib"),
            _conda("lerc-4.2.0-h1eee2c3_0.conda", 166477, "c97aa17d16d2ac332ba9f184e82ce8f72dcb10e9a10c5f299030be2d44e191b9",
                   "lib/libLerc.4.dylib"),
            _conda("libarchive-3.8.9-gpl_h76147b0_101.conda", 798787, "cee5639cdfc5121339516e2cdf2f8338e376ca6ac42a45bba77b8b7e199b8305",
                   "lib/libarchive.13.dylib"),
            _conda("libcurl-8.22.0-h6651222_0.conda", 419925, "46def32ec20d4dc6bce75d90fe5f2465a71cc22acef608c5ba37cc392e80e01d",
                   "lib/libcurl.4.dylib"),
            _conda("libcxx-23.1.3-h55c6f16_0.conda", 577501, "2d9d31172d7919bd276a8d1be338de567dd5afdd181e8d3d3d8c2d8695e09de2",
                   "lib/libc++.1.0.dylib>libc++.1.dylib"),
            _conda("libdeflate-1.25-he7e0567_1.conda", 55727, "d896f4aa4ce4c590c2838678cb1917356fdb461d2a189991c0280c818c362172",
                   "lib/libdeflate.0.dylib"),
            _conda("libiconv-1.18-he4c29f2_3.conda", 750816, "689a14968267f2f97c07112fca5636e7d756036b9aee969911267c20bd3eea1f",
                   "lib/libiconv.2.dylib"),
            _conda("libjpeg-turbo-3.2.0-h84a0fba_1.conda", 558459, "05006418f9392c9b723e8428808db106de0350a497832f95f921b85ff1072310",
                   "lib/libjpeg.8.3.2.dylib>libjpeg.8.dylib"),
            _conda("liblzma-5.8.3-h8088a28_1.conda", 91720, "23d0630046a3e8b164d8f80f2b74ed2605af2e7050ab9913018056402fae4311",
                   "lib/liblzma.5.dylib"),
            _conda("libnghttp2-1.68.1-h435687b_1.conda", 567024, "445440d8cccccb6585f71d1eb7c949d64a2af2ced5481d924621424ff3b6a398",
                   "lib/libnghttp2.14.dylib"),
            _conda("libpng-1.6.59-hf5e6511_0.conda", 290299, "4de4dc4cb7432c328c1eaf4f7cc9c6b48c073eab1e9c6723d4533e6d57e7d595",
                   "lib/libpng16.16.dylib"),
            _conda("libpsl-0.23.1-hdb0c161_1.conda", 72673, "c8c3153df39abedf3413483f672151ad70f1adb0c06c192bdf7ec432c3616b07",
                   "lib/libpsl.5.dylib"),
            _conda("libssh2-1.11.1-hbf2880b_1.conda", 280492, "a056e518c3d2d09fbb1778cea80c0c95338fb24adf1a817bbf90fb484850a304",
                   "lib/libssh2.1.0.1.dylib>libssh2.1.dylib"),
            _conda("libtiff-4.7.2-hf67920b_1.conda", 380645, "847d22a86ae28f66d3888702698254d25bb4eac3a0f64c1fabfb1842ac00be4c",
                   "lib/libtiff.6.dylib"),
            _conda("libwebp-base-1.6.0-h202fb40_1.conda", 294522, "0ff54650d470c7e54cbeffdd53a8c063e055a7bcf784388c0385ce5c4741b0f4",
                   "lib/libsharpyuv.0.1.2.dylib>libsharpyuv.0.dylib", "lib/libwebp.7.2.0.dylib>libwebp.7.dylib", "lib/libwebpmux.3.1.2.dylib>libwebpmux.3.dylib"),
            _conda("libxml2-16-2.15.4-heb56d2d_1.conda", 469239, "1acd4dd49c6f54440dc172ab469d0e2c7c3c02de5b1f21158a233eb585ec3c38",
                   "lib/libxml2.16.dylib"),
            _conda("libzlib-1.3.2-h8088a28_3.conda", 47822, "a18fa5d5bac452401459f966cf0d872224e8080c4ff93c77e168d43ab42ef9d7",
                   "lib/libz.1.3.2.dylib>libz.1.dylib"),
            _conda("llvm-openmp-23.1.3-hdb3d66b_0.conda", 287857, "b8114cade9167d9f0ec927af9b0552b035777c231742d16a18f66bbb8661a303",
                   "lib/libomp.dylib"),
            _conda("lz4-c-1.10.0-hdca3b7d_2.conda", 171838, "1887867b393eb3c2b336deaffcf228574821ea1e0dcb9ef7bf6c9f59cec40f03",
                   "lib/liblz4.1.10.0.dylib>liblz4.1.dylib"),
            _conda("lzo-2.10-h74c22ad_1003.conda", 154521, "95d5f16bfa62ff50980a5e0329920bb2b5ec66d0e0d6c87954603713c3519ece",
                   "lib/liblzo2.2.0.0.dylib>liblzo2.2.dylib"),
            _conda("openjpeg-2.5.4-h4d1e80c_2.conda", 377958, "e71bd04b5d0e39f39a3c039d021d3b76cd65bee3032e7f859ab7082f0ace8be5",
                   "lib/libopenjp2.2.5.4.dylib>libopenjp2.7.dylib"),
            _conda("openssl-3.6.5-h55eecbc_0.conda", 3111573, "44fbc526456571a9fb8fa18157f6f269d4d4bd40eef20183a93b9c9296f94497",
                   "lib/libcrypto.3.dylib", "lib/libssl.3.dylib"),
            _conda("zstd-1.5.7-hf451053_7.conda", 433687, "da867f5092eb0cb746d353694f0098031fd9817a4ce7d5743121209ae0f406ca",
                   "lib/libzstd.1.5.7.dylib>libzstd.1.dylib"),
        ),
    ),
    "windows-x64": OcrManifest(
        subdir="win-64",
        tesseract=TesseractStream(
            "tesseract-5.5.3-he87eeb8_0.conda", 181, 178930274,
            ("Library/bin/tesseract.exe", 68096, "dbb075afe525e32639d9a62133100c512e0a3b88e2c45e06bfad8683cd3f5c34"),
            ("Library/bin/tesseract55.dll", 2907648, "cde298f5b430c523255e8837065ed0f9cd694ca91e080403f0c8ce7493efe5dc"),
        ),
        packages=(
            _conda("bzip2-1.0.8-h0ad9c76_10.conda", 55919, "04767466ee9227c9c57ab2c6503e0149177d34111c7418d2f420297acb1eb229",
                   "Library/bin/libbz2.dll"),
            _conda("giflib-5.2.2-hb0bd012_1.conda", 84682, "573af9bb6294d125a0be6dc4963e589188da92426c3887feb60dd22a07530e4f",
                   "Library/bin/gif-7.dll"),
            _conda("icu-78.3-h5112557_2.conda", 16835644, "75c549b55b673e15de8785a8e5dd85bca7eb612eee0ff4dc8d7bdaa15eacbdbb",
                   "Library/bin/icudt78.dll", "Library/bin/icuuc78.dll"),
            _conda("leptonica-1.87.0-hb83fb89_1.conda", 1458170, "6a0e2e099c4b530300fdcc092ddd0ffcc5383dbd04cdf03b232996857b3017d8",
                   "Library/bin/leptonica-1.87.0.dll"),
            _conda("lerc-4.2.0-hd936e49_0.conda", 175297, "93d666f63f284ef77b87b0b1f77b70f7d36d315a132f9afa64bc0012d937ba39",
                   "Library/bin/Lerc.dll"),
            _conda("libarchive-3.8.9-gpl_h8fdc8af_101.conda", 1151402, "71cb6e49282d2aee1eb0249a290dc2333b0063ac2ecec69116ad569d822a0aaf",
                   "Library/bin/archive.dll"),
            _conda("libcurl-8.22.0-hdb0ef4a_0.conda", 413226, "5fe063cffa90ad3ef04e25c76b1a79cdbc4f6c43747164a7dcdd89c4faebb84e",
                   "Library/bin/libcurl.dll"),
            _conda("libdeflate-1.25-h1a1d4e4_1.conda", 157828, "af1cda21d4653f594fbef20aa4e1ff158a546902b3307ef8af9a9b44b43862d2",
                   "Library/bin/deflate.dll"),
            _conda("libiconv-1.18-hc1393d2_3.conda", 694899, "35e04e3ddac7720fc7c550a1c9f998604299d6a7bd1f3ec9b2825d825061daa2",
                   "Library/bin/charset.dll", "Library/bin/iconv.dll"),
            _conda("libjpeg-turbo-3.2.0-hfd05255_1.conda", 990125, "df78ab4c0eecb3dd9331898f96baeed8e5ca1c363346332517abeb0b614b9a53",
                   "Library/bin/jpeg8.dll"),
            _conda("liblzma-5.8.3-hfd05255_1.conda", 105809, "d36c4a1e1f80fd08e18a407e03622ff2f34dfdd022da6488ad19603dea19e6d5",
                   "Library/bin/liblzma.dll"),
            _conda("libpng-1.6.59-hdc8cecf_0.conda", 385760, "36050852deae6edc98d7b71489b8d07254e2753c69bd524bf5784927cd1f0b2c",
                   "Library/bin/libpng16.dll"),
            _conda("libpsl-0.23.1-h9b16d47_1.conda", 73511, "e53fe3b09f82b59b644264133d6d148ac31bb5451b7583cff55690bf038f2f62",
                   "Library/bin/psl-5.dll"),
            _conda("libssh2-1.11.1-h734d217_1.conda", 295149, "1097b08429b4f53f5751a32c6d99a083d227ca7f7812c0b239eea124cec073a0",
                   "Library/bin/libssh2.dll"),
            _conda("libtiff-4.7.2-h8f73337_1.conda", 1014211, "3575a092e3e52625a1767804a4ebd321becaadf61b63dcd6735ebb88c0b3c359",
                   "Library/bin/tiff.dll"),
            _conda("libwebp-base-1.6.0-h4d5522a_1.conda", 278886, "4470d98d3178b0d45492eed4808afe421d2e7808352b8d06c2dc29166b75d94d",
                   "Library/bin/libsharpyuv.dll", "Library/bin/libwebp.dll", "Library/bin/libwebpmux.dll"),
            _conda("libxml2-16-2.15.4-h3cfd58e_1.conda", 515690, "819c4edd7b7bcfa9461aeccb56c0a7e9fa7c251163333e64222c142499822958",
                   "Library/bin/libxml2.dll"),
            _conda("libzlib-1.3.2-hfd05255_3.conda", 58529, "0629c2cc0404d3bb29d6baa7b4ba62da80797015e86de050db81ea5a07050527",
                   "zlib.dll"),
            _conda("lz4-c-1.10.0-h6a83c73_2.conda", 150673, "6824e36be0f95ae516dc36dddd18f69cbad0e4e07906fcb10a5f26a8dc471903",
                   "Library/bin/lz4.dll"),
            _conda("openjpeg-2.5.4-h90fa87c_2.conda", 274994, "d1c630ccd9ae0898b48d84e986f4c330f66a25e535f99efe8cbf03f042b33da5",
                   "Library/bin/openjp2.dll"),
            _conda("openssl-3.6.5-hf411b9b_0.conda", 9402647, "bc44625e0c6d83f3e02b5f6adc01e0e3ca7001d3d94e24959591a57f5a011b9a",
                   "Library/bin/libcrypto-3-x64.dll"),
            _conda("vc14_runtime-14.51.36247-habf1de7_41.conda", 767955, "4e4cb599cdc41bf2109d1464c127b5bcbddf548ce3e322e612afb691338b48f8",
                   "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"),
            _conda("zstd-1.5.7-h534d264_7.conda", 387535, "ca7daae4f218a11fab82cc2857f0ea518ec3f46acec60490485347a4c22c6b3e",
                   "Library/bin/zstd.dll"),
        ),
    ),
    # 不发安装包，只给在 Linux 上开发、测试的人用。
    "linux-x64": OcrManifest(
        subdir="linux-64",
        tesseract=TesseractStream(
            "tesseract-5.5.3-h7618cdf_0.conda", 181, 180991971,
            ("bin/tesseract", 65888, "4cb12efe9cfbcd5541fe8926ccfad71162cb22bb9cd5a6a5895e5878fe5fa007"),
            ("lib/libtesseract.so.5.0.5>libtesseract.so.5", 4490736, "ec0849aafaf4c1005d11ea1a0fcd269298aa3e24b45c1cdb868d77db9d2a2e8d"),
        ),
        packages=(
            _conda("bzip2-1.0.8-hda65f42_10.conda", 257808, "1a0d382c515ebf55f8ee1f38c8b81bc95af5c2acc42ad53b66bc5df932032f96",
                   "lib/libbz2.so.1.0.8>libbz2.so.1.0"),
            _conda("giflib-5.2.2-ha257d8a_1.conda", 77403, "3611c5d605cd56017704e6c01c7bd849e74a656a546d4c49d655fde49ddfeecc",
                   "lib/libgif.so.7.2.0>libgif.so.7"),
            _conda("icu-78.3-py310h44b86e0_2.conda", 14459115, "9f07834f0c546ab14d885ce0366285f61f44e326c0edd1fc63b8294e113ae432",
                   "lib/libicudata.so.78.3>libicudata.so.78", "lib/libicuuc.so.78.3>libicuuc.so.78"),
            _conda("keyutils-1.6.3-h7cc23a3_1.conda", 135295, "dd053c96dcb0dcfd59422aefea9d2fe937a190167f34fba7893ec1e10a7e8963",
                   "lib/libkeyutils.so.1.10>libkeyutils.so.1"),
            _conda("krb5-1.22.2-hbc21106_2.conda", 1394333, "2a5c38c85e63df84c4e69ee71439841ce570d259ae3060627bb9a49a938d66f4",
                   "lib/libcom_err.so.3.0>libcom_err.so.3", "lib/libgssapi_krb5.so.2.2>libgssapi_krb5.so.2", "lib/libk5crypto.so.3.1>libk5crypto.so.3", "lib/libkrb5.so.3.3>libkrb5.so.3", "lib/libkrb5support.so.0.1>libkrb5support.so.0"),
            _conda("leptonica-1.87.0-h12c84f9_1.conda", 2615553, "e822d7d1f8e610b2c9db244389ee0e87840a18c8b5f0f82b7393ee182a248dd4",
                   "lib/libleptonica.so.6.0.0>libleptonica.so.6"),
            _conda("lerc-4.2.0-hdb68285_0.conda", 271158, "bf9fdebf55d8bc99d83531cdffda00703f4dc5f93a1a956768c147362c72feda",
                   "lib/libLerc.so.4"),
            _conda("libarchive-3.8.9-gpl_h3152399_101.conda", 875346, "bfae027a67d02325cd6b75edd60beb67cf24483af9df0ae9ef74d4438e41c806",
                   "lib/libarchive.so.13.8.9>libarchive.so.13"),
            _conda("libcurl-8.22.0-ha042cf0_0.conda", 499651, "483b7eb97f56fbb3a9cb7190d33012dfa0a5aaed99ede61465563cd97dd82504",
                   "lib/libcurl.so.4.8.0>libcurl.so.4"),
            _conda("libdeflate-1.25-hd45a770_1.conda", 73710, "82e134c8a08b1eed9a2ed8ab578b89aa1730dcde3dea8dd87645ed0637878e54",
                   "lib/libdeflate.so.0"),
            _conda("libgcc-16.2.0-ha9f2e26_7.conda", 1058444, "e031634c3a928f9594eba868bc46ef5230d6571a364a0c63215b06c4563bfcea",
                   "lib/libgcc_s.so.1"),
            _conda("libgomp-16.2.0-he0feb66_7.conda", 640566, "92a0bb350c7be6a4eedcb4e111c60aefd176018e6b1c009a6e025dbe9bbcd758",
                   "lib/libgomp.so.1.0.0>libgomp.so.1"),
            _conda("libiconv-1.18-h0cb94f2_3.conda", 789471, "f943117edb9cd4d9c61cc972eee5a34291dc55ea7a6e9e38da104995841cbcb6",
                   "lib/libiconv.so.2.7.0>libiconv.so.2"),
            _conda("libjpeg-turbo-3.2.0-hb03c661_1.conda", 650434, "bba8538e6538ed58a8479b332337b96986561f975d06cfa2039a016c2d246ee4",
                   "lib/libjpeg.so.8.3.2>libjpeg.so.8"),
            _conda("liblzma-5.8.3-hb03c661_1.conda", 112995, "9787df8c22a59c9a70d3e5a10db9ad663485e75e9ccc3f09bd092cb7b95e0dab",
                   "lib/liblzma.so.5"),
            _conda("libnghttp2-1.68.1-h74cf4be_1.conda", 649974, "11a2f54a6f391e25738bce0023e6ef499600147bbcf21c1fa69d2e251b03cc6a",
                   "lib/libnghttp2.so.14.29.3>libnghttp2.so.14"),
            _conda("libpng-1.6.59-h922cc85_0.conda", 316872, "c7381be8ef713501f6cd2f5ad473e401f2edeaec3c46788296b8dbf0de165e15",
                   "lib/libpng16.so.16.59.0>libpng16.so.16"),
            _conda("libpsl-0.23.1-hd9e3e90_1.conda", 72519, "09e8effdc89bc5d021349318d32b85594f5b8d8e3ab8f90a85b81f8fe695a566",
                   "lib/libpsl.so.5.3.8>libpsl.so.5"),
            _conda("libssh2-1.11.1-h6154650_1.conda", 306045, "7e463000fa1cba3f3a6597b776f6ab301d425a96e3bc20d0d9d76a3b9f237aa3",
                   "lib/libssh2.so.1.0.1>libssh2.so.1"),
            _conda("libstdcxx-16.2.0-h934c35e_7.conda", 6615436, "fa7018298629fc429971fffd6ecba3d27a7b02e4e890d6a93ea01ffebe8cb745",
                   "lib/libstdc++.so.6.0.36>libstdc++.so.6"),
            _conda("libtiff-4.7.2-hcc2c06a_1.conda", 459753, "10e125da82ca93e09191e66e5a94d566b2cf8d4024e2a0db3a9114297447e0d9",
                   "lib/libtiff.so.6.3.0>libtiff.so.6"),
            _conda("libwebp-base-1.6.0-hd42ef1d_1.conda", 428430, "8415001414f488c85b72b9d8cc2071dfb3981a47bc3c8eb56ef91a57d12eae7f",
                   "lib/libsharpyuv.so.0.1.2>libsharpyuv.so.0", "lib/libwebp.so.7.2.0>libwebp.so.7", "lib/libwebpmux.so.3.1.2>libwebpmux.so.3"),
            _conda("libxml2-16-2.15.4-hf3af7cc_1.conda", 569062, "d1906463070c21f3215cd5de93164f31c44d5bac6ae9b5d8f5adf42b1e304039",
                   "lib/libxml2.so.16.1.4>libxml2.so.16"),
            _conda("libzlib-1.3.2-h25fd6f3_3.conda", 63713, "eb8a0db0aa570124f7d2a93d7c7f596e3390df5e047818d873baad32985fc736",
                   "lib/libz.so.1.3.2>libz.so.1"),
            _conda("lz4-c-1.10.0-hee9eb32_2.conda", 181812, "b8c0e930183e6b7f83cd35ace102f2b8c2f0faae41dc05255dc72f247dfc556a",
                   "lib/liblz4.so.1.10.0>liblz4.so.1"),
            _conda("lzo-2.10-hebe6cf0_1003.conda", 191573, "a0a1400198e5302d8367f2ed53437ee3cc41dbc463cb481921ce765578780b7a",
                   "lib/liblzo2.so.2.0.0>liblzo2.so.2"),
            _conda("openjpeg-2.5.4-heb1ab33_2.conda", 391242, "131688a88bea8da92fe418c4558c5073435e2848911bc4c836c3a9dd5994b4aa",
                   "lib/libopenjp2.so.2.5.4>libopenjp2.so.7"),
            _conda("openssl-3.6.5-h781a0a9_0.conda", 3195299, "9c1533f1cf784f0e250919c0c9762d0b0e6bc727a79290ffb7cea1ceca1d038c",
                   "lib/libcrypto.so.3", "lib/libssl.so.3"),
            _conda("zstd-1.5.7-hb78ec9c_7.conda", 601301, "47d682b9f6d6ec9eb1a6e6c3e75ea6273e899e78fb7fc59f81d39745009fbc60",
                   "lib/libzstd.so.1.5.7>libzstd.so.1"),
        ),
    ),
}

TESSDATA = (
    _tessdata("eng", 4113088, "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"),
    _tessdata("fra", 1130365, "ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca"),
    _tessdata("deu", 1525436, "19d219bbb6672c869d20a9636c6816a81eb9a71796cb93ebe0cb1530e2cdb22d"),
    _tessdata("spa", 2294433, "6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464"),
    _tessdata("ita", 2701314, "b8f89e1e785118dac4d51ae042c029a64edb5c3ee42ef73027a6d412748d8827"),
    _tessdata("por", 1982756, "c4932b937207a9514b7514d518b931a99938c02a28a5a5a553f8599ed58b7deb"),
)

ProgressFn = Callable[[str, int, int], None]  # (phase, done, total)

# tesseract 程序在包开头约 1.4 MB 处就取齐了；进度条按这个估。
_STREAM_ESTIMATE = 2 * 1024 * 1024


class ComponentInstallError(RuntimeError):
    """一键安装没有成功；消息是给用户看的一句话。"""


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x64"
    return ""


def manifest() -> OcrManifest | None:
    return MANIFEST.get(platform_key())


def download_size() -> int:
    current = manifest()
    if current is None:
        return 0
    return sum(item.size for item in current.packages) + sum(item.size for item in TESSDATA) + _STREAM_ESTIMATE


def languages() -> list[str]:
    return [item.name.split(".")[0] for item in TESSDATA]


def component_root() -> Path:
    return get_app_data_dir() / "plugins" / "ocr-component"


def _record_path() -> Path:
    return component_root() / "installed.json"


def _read_record() -> dict[str, Any]:
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _executable_name() -> str:
    return "tesseract.exe" if sys.platform == "win32" else "tesseract"


def installed_executable() -> Path | None:
    """一键安装装好的 tesseract；没装或文件已缺失时返回 None。"""
    relative = str(_read_record().get("dir") or "").strip()
    if not relative or "/" in relative or "\\" in relative or relative.startswith("."):
        return None
    base = component_root() / relative
    executable = base / "bin" / _executable_name()
    return executable if executable.is_file() and (base / "tessdata").is_dir() else None


def tessdata_dir(executable: Path) -> Path | None:
    """executable 是一键安装的 tesseract 时，返回它的语言数据目录。"""
    installed = installed_executable()
    if installed is None:
        return None
    try:
        if executable.resolve() != installed.resolve():
            return None
    except OSError:
        return None
    return installed.parent.parent / "tessdata"


def installed_info() -> dict[str, Any]:
    record = _read_record()
    executable = installed_executable()
    return {
        "installed": executable is not None,
        "path": str(executable or ""),
        "version": str(record.get("version") or ""),
        "tesseract": str(record.get("tesseract") or ""),
        "outdated": executable is not None and str(record.get("version") or "") != MANIFEST_VERSION,
    }


def _write_record(data: dict[str, Any]) -> None:
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _destination(source: str) -> tuple[str, str]:
    """包内路径 -> (包内真实路径, 装好后的相对路径)。"""
    real, _, rename = source.partition(">")
    leaf = rename or PurePosixPath(real).name
    if sys.platform == "win32" or real.startswith("bin/"):
        return real, f"bin/{leaf}"
    return real, f"lib/{leaf}"


_INSTALL_LOCK = threading.Lock()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _http_client():
    """测试替换成假的下载源；平时由下载函数自己建、自己关。"""
    return None


def _self_test(base: Path) -> str:
    """装好的程序能启动、能列出语言、能认出一行字；返回版本号，失败抛 ComponentInstallError。"""
    from PIL import Image, ImageDraw, ImageFont

    executable = base / "bin" / _executable_name()
    env = {**os.environ, "TESSDATA_PREFIX": str(base / "tessdata")}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(executable), *args], capture_output=True, text=True, timeout=60,
                              check=False, env=env, creationflags=_NO_WINDOW)

    try:
        version = run("--version")
        langs = run("--list-langs")
        image = Image.new("L", (900, 160), 255)
        ImageDraw.Draw(image).text((20, 40), "Plan de masse 2026", fill=0, font=ImageFont.load_default(size=56))
        sample = base / "self-test.png"
        image.save(sample)
        try:
            ocr = run(str(sample), "stdout", "--psm", "11", "-l", "fra+eng", "tsv")
        finally:
            sample.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"[文字识别组件] 一键安装后试运行失败：{exc!r}")
        raise ComponentInstallError("组件已下载，但无法在本机运行。") from exc
    listed = {line.strip() for line in langs.stdout.splitlines()}
    if version.returncode != 0 or langs.returncode != 0 or not set(languages()) <= listed \
            or ocr.returncode != 0 or "masse" not in ocr.stdout:
        detail = (version.stderr or langs.stderr or ocr.stderr or ocr.stdout)[-800:]
        logger.warning(f"[文字识别组件] 一键安装后试运行失败：{detail}")
        raise ComponentInstallError("组件已下载，但无法在本机运行。")
    first = (version.stdout or version.stderr).splitlines()[:1]
    return first[0].replace("tesseract", "").strip() if first else TESSERACT_VERSION


def install(*, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """下载、核对、解压、试识别，全部通过才换上新目录。"""
    current = manifest()
    if current is None:
        raise ComponentInstallError("当前系统不支持一键安装。")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise ComponentInstallError("组件正在安装，请稍候。")
    root = component_root()
    staging = root / f".staging-{uuid.uuid4().hex[:8]}"
    downloads = root / ".downloads"
    total = download_size()
    try:
        client = _http_client()
        packages = [conda_file(current.subdir, item.name, item.size, item.sha256) for item in current.packages]
        remote = [*packages, *TESSDATA]
        done_files = sum(item.size for item in remote)

        def on_bytes(done: int, _total: int) -> None:
            if progress:
                progress("download", min(done, total), total)

        def on_stream(received: int) -> None:
            if progress:
                progress("download", min(done_files + received, total), total)

        try:
            paths = download_files(remote, downloads, progress=on_bytes, cancel=cancel, client=client)
            staging.mkdir(parents=True, exist_ok=True)
            stream = current.tesseract
            files = dict(_destination(source) for source, _size, _sha in stream.files)
            hashes = {_destination(source)[0]: (size, sha) for source, size, sha in stream.files}
            stream_conda_files(
                conda_file(current.subdir, stream.name, 0, ""), stream.data_offset, stream.data_size,
                staging, files, hashes, progress=on_stream, cancel=cancel, client=client)
            for index, (package, path) in enumerate(zip(current.packages, paths), start=1):
                if progress:
                    progress("extract", index - 1, len(current.packages))
                extract_conda(path, staging, dict(_destination(source) for source in package.files))
        except ComponentDownloadError as exc:
            raise ComponentInstallError(str(exc)) from exc
        tessdata = staging / "tessdata"
        (tessdata / "configs").mkdir(parents=True, exist_ok=True)
        for item, path in zip(TESSDATA, paths[len(packages):]):
            shutil.copyfile(path, tessdata / item.name)
        for name, text in _CONFIGS.items():
            (tessdata / "configs" / name).write_text(text, encoding="utf-8")
        if progress:
            progress("verify", 0, 1)
        version = _self_test(staging)
        final_dir = root / f"{MANIFEST_VERSION}-{uuid.uuid4().hex[:8]}"
        old_dirs = [path for path in root.iterdir() if path.is_dir() and path not in (staging, downloads)]
        staging.replace(final_dir)
        _write_record({
            "version": MANIFEST_VERSION,
            "dir": final_dir.name,
            "tesseract": version,
            "platform": platform_key(),
        })
        for old in old_dirs:
            _discard(old)
        shutil.rmtree(downloads, ignore_errors=True)
        if progress:
            progress("done", 1, 1)
        return installed_info()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _INSTALL_LOCK.release()


def _discard(path: Path) -> None:
    trash = path.with_name(f".trash-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(trash)
    except OSError:
        trash = path
    shutil.rmtree(trash, ignore_errors=True)


def uninstall() -> None:
    """删掉一键安装的组件（正在运行的识别进程结束后才删得掉的，下次安装或卸载时再删）。"""
    root = component_root()
    _record_path().unlink(missing_ok=True)
    if not root.is_dir():
        return
    for path in list(root.iterdir()):
        if path.is_dir():
            _discard(path)
