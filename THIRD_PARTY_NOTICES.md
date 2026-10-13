# 第三方组件许可

Translator 自己的代码按 [LICENSE](LICENSE)（MIT）发布。下面这些第三方组件随程序一起分发，
各自沿用原来的许可（许可全文见各自的源码仓库），MIT 不覆盖它们。

| 组件 | 用途 | 许可 | 源码 |
| --- | --- | --- | --- |
| PyMuPDF | PDF「原位替换」「转成 Word 再翻译」（Beta）读写 PDF | AGPL-3.0 | https://github.com/pymupdf/PyMuPDF |
| pdf2docx | PDF「转成 Word 再翻译」（Beta）把 PDF 转成 .docx | MIT | https://github.com/ArtifexSoftware/pdf2docx |
| OpenCV（opencv-python-headless） | pdf2docx 的依赖 | Apache-2.0 | https://github.com/opencv/opencv-python |
| NumPy | pdf2docx 的依赖 | BSD-3-Clause | https://github.com/numpy/numpy |
| fontTools | pdf2docx 的依赖 | MIT | https://github.com/fonttools/fonttools |
| Tesseract 词表（core/pdf_vector/lexicon） | 原位替换判断识别出的词是否成词 | Apache-2.0 | https://github.com/tesseract-ocr/tessdata_fast |

PyMuPDF 按 AGPL-3.0 使用：本程序没有修改它，用的是 PyPI 上发布的原版；它的完整源码见上面
的链接。Tesseract OCR 本身不随程序分发，用户需要时自行安装（见「插件」页）。
