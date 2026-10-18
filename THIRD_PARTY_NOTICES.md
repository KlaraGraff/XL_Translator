# 第三方组件许可

Translator 自己的代码按 [LICENSE](LICENSE)（MIT）发布。下面这些第三方组件随程序一起分发，
各自沿用原来的许可（许可全文见各自的源码仓库），MIT 不覆盖它们。

| 组件 | 用途 | 许可 | 源码 |
| --- | --- | --- | --- |
| Tesseract 词表（core/pdf_vector/lexicon） | 原位替换判断识别出的词是否成词 | Apache-2.0 | https://github.com/tesseract-ocr/tessdata_fast |
| pypinyin 汉字读音表（core/pinyin_readings.py，由 pypinyin 0.55.0 的 pinyin_dict 生成） | 核对译文里人名的汉字和原文拼音是否对得上 | MIT | https://github.com/mozillazg/python-pinyin |

## 不随程序分发的可选组件

下面这些组件不在安装包里。用户需要时在「插件」页安装：「一键安装」从原发布渠道（PDF 处理组件
来自 PyPI，文字识别组件来自 conda-forge 和 tessdata_fast，官方源连不上时用国内镜像；图纸转换工具
来自 ODA 官网）下载原版文件，核对文件大小和 SHA-256（图纸转换工具核对 ODA 的数字签名）后解压到
本机的应用数据目录；也可以自行安装，再用「选择安装位置」连接。它们各自沿用原来的许可，MIT 不覆盖它们。

| 组件 | 所属插件 | 用途 | 许可 | 源码 |
| --- | --- | --- | --- | --- |
| PyMuPDF | PDF 处理组件 | PDF「原位替换」、页面分类和「转成 Word 再翻译」读写 PDF | AGPL-3.0 | https://github.com/pymupdf/PyMuPDF |
| pdf2docx | PDF 处理组件 | PDF 转成 Word | MIT | https://github.com/ArtifexSoftware/pdf2docx |
| OpenCV（opencv-python-headless） | PDF 处理组件 | pdf2docx 的依赖 | Apache-2.0 | https://github.com/opencv/opencv-python |
| NumPy | PDF 处理组件 | pdf2docx 的依赖 | BSD-3-Clause | https://github.com/numpy/numpy |
| fontTools | PDF 处理组件 | pdf2docx 的依赖 | MIT | https://github.com/fonttools/fonttools |
| Tesseract OCR | 文字识别组件 | 识别图纸里画成线条的文字 | Apache-2.0 | https://github.com/tesseract-ocr/tesseract |
| tessdata_fast（英、法、德、西、意、葡） | 文字识别组件 | Tesseract 的识别数据 | Apache-2.0 | https://github.com/tesseract-ocr/tessdata_fast |
| ODA File Converter | 图纸转换工具 | DWG 和 DXF 互转 | Open Design Alliance 的专有许可（免费提供，不是开源软件；一键安装前由用户确认 ODA 的使用条款） | https://www.opendesign.com/guestfiles/oda_file_converter |
| Tesseract 用到的库（conda-forge 编译版） | 文字识别组件 | Leptonica、libarchive、libcurl、OpenSSL、ICU、libxml2、libtiff、libpng、libjpeg-turbo、libwebp、OpenJPEG、giflib、zlib、zstd、lz4、xz、bzip2、LZO、libiconv、Kerberos 等 | 各自的许可（多为 BSD、MIT、Apache-2.0；LZO 为 GPL-2.0，libiconv 为 LGPL） | https://conda-forge.org |

PyMuPDF 按 AGPL-3.0 使用：本程序没有修改它，一键安装下载的是 PyPI 上发布的原版；它的完整
源码见上面的链接。Translator 在单独的进程里调用 pdf2docx，并在需要时加载用户本机已安装的
PyMuPDF。Tesseract OCR 以单独的程序运行，Translator 只调用它的命令行；一键安装取的是
conda-forge 发布的原样文件，没有修改。ODA File Converter 不随本程序分发：一键安装由用户确认
ODA 的使用条款后，从 ODA 官网的公开下载地址直接下载到用户自己的电脑，不经过第三方，不做修改。
