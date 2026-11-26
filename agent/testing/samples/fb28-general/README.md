# PDF 转 Word 通用测试样本（反馈 #28 之外）

用 LaTeX 生成的公开内容，没有用户数据。用来确认 #28 的修复不只对那本讲义有效。

- `math.pdf`（6 页）：Latin Modern 字体，蓝、绿、橙三种彩色框，一个黑底白字框，公式、真值表、(i)(ii) 编号。
- `code.pdf`（5 页）：Python / C 代码块，伪代码，`is_empty(S)` 这类行内标识符，git 命令表。

重新生成：`pdflatex math.tex` 和 `pdflatex code.tex`（各跑两遍）。
