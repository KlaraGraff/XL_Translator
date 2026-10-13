# 第三方组件许可

Translator 自己的代码按 [LICENSE](LICENSE)（MIT）发布。下面这些第三方组件随程序一起分发，
各自沿用原来的许可（许可全文见各自的源码仓库），MIT 不覆盖它们。

| 组件 | 用途 | 许可 | 源码 |
| --- | --- | --- | --- |
| Tesseract 词表（core/pdf_vector/lexicon） | 原位替换判断识别出的词是否成词 | Apache-2.0 | https://github.com/tesseract-ocr/tessdata_fast |

## 不随程序分发的可选组件

下面这些组件不在安装包里。用户需要时在「插件」页安装：「一键安装」从 PyPI（或它的国内镜像）
下载原版发布文件，核对文件大小和 SHA-256 后解压到本机的应用数据目录；也可以自行安装，再用
「选择安装位置」连接。它们各自沿用原来的许可，MIT 不覆盖它们。

| 组件 | 所属插件 | 用途 | 许可 | 源码 |
| --- | --- | --- | --- | --- |
| PyMuPDF | PDF 处理组件 | PDF「原位替换」、页面分类和「转成 Word 再翻译」读写 PDF | AGPL-3.0 | https://github.com/pymupdf/PyMuPDF |
| pdf2docx | PDF 处理组件 | PDF 转成 Word | MIT | https://github.com/ArtifexSoftware/pdf2docx |
| OpenCV（opencv-python-headless） | PDF 处理组件 | pdf2docx 的依赖 | Apache-2.0 | https://github.com/opencv/opencv-python |
| NumPy | PDF 处理组件 | pdf2docx 的依赖 | BSD-3-Clause | https://github.com/numpy/numpy |
| fontTools | PDF 处理组件 | pdf2docx 的依赖 | MIT | https://github.com/fonttools/fonttools |
| Tesseract OCR | 文字识别组件 | 扫描页文字识别 | Apache-2.0 | https://github.com/tesseract-ocr/tesseract |

PyMuPDF 按 AGPL-3.0 使用：本程序没有修改它，一键安装下载的是 PyPI 上发布的原版；它的完整
源码见上面的链接。Translator 在单独的进程里调用 pdf2docx，并在需要时加载用户本机已安装的
PyMuPDF。
