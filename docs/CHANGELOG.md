# Translator 版本更新日志

## 未发布

- **界面焕新（第一步）：新配色「流光」、新侧栏、页面切换动效、可输入搜索的下拉框**：默认配色换成「流光」（电光蓝主色，青色只用于进行中），原来的五套配色仍在「设置 → 外观与语言」里可选；以前用默认「素雅灰」的，升级后自动换成流光，手动选过其他配色的不变（settings schema v41）。侧栏去掉「资源」分组标题，四种翻译类型收到「翻译」下面、缩进显示，选中项有一块滑动的高亮；内容区改为一张白色底板，顶栏不再单独成条；圆角、按钮、开关、表格表头统一调整。切页时旧页面淡出、新页面从下方淡入；弹出菜单和对话框有轻微的弹出动效；系统开启「减少动态效果」时全部关闭。所有下拉框和语言选择框可以直接输入筛选（匹配名称、说明或代码，匹配处高亮），↑↓ 选择、回车确认、Esc 还原，点到别处时按输入的内容选中唯一匹配项，否则还原。顶栏「N 个任务运行中」改为「N 个任务进行中」。

  **Interface refresh (step one): new Lumen palette, new sidebar, page transitions and type-to-search dropdowns**: The default palette is now Lumen (electric-blue accent, cyan only for work in progress); the five earlier palettes remain in Settings > Appearance & language. Users on the old default Neutral grey move to Lumen on upgrade; anyone who picked another palette keeps it (settings schema v41). The sidebar drops the Resources heading and nests the four translation types under Translation, with a sliding highlight on the selected item; the content area becomes one white sheet with the top bar inside it; corner radii, buttons, switches and table headers are aligned. Switching pages fades the old page out and the new one in from below; menus and dialogs pop in gently; all of this is off when the system's Reduce motion setting is on. Every dropdown and language picker accepts typing to filter (by name, description or code, with the match highlighted); ↑↓ moves, Enter picks, Esc restores, and clicking away picks the only remaining match or restores the previous value. The top bar's "N tasks running" now reads "N tasks in progress".

- **界面焕新（第二步）：工作区重排**：四个翻译页的来源栏改为页面顶部一整条路径栏（粘贴路径回车即读取，右侧「选择…」选文件或文件夹，读过后有「重新读取」）；还没有来源时下面是投放区（大图标、支持的扩展名、「选择文件」「选择文件夹」），读取中显示骨架占位。统计条固定四块，右边一句安静说明（如「相同内容只翻译一次」；Excel 有旧版 .xls 时改为提示开始时选择转换方式），读完后错落浮现；下面一行就绪说明写明待翻译处数、记忆库优先复用、开始后自动识别场景与原文语言。运行中改为一个进度框：Excel、Word、PDF 显示「读取文件 → 翻译 → 生成文件」流程步骤（Excel 调整行高时多一步），进度条带流光，运行日志收进可折叠的一栏；完成后列表上方是一张结果卡（成功 / 需复核 / 失败三种颜色，「继续翻译」「打开输出文件夹」「查看报告」「反馈此任务」都在卡上），右栏底部换成「再翻译一次」。右栏瘦身：顶部一张配方摘要（如「自动识别 → 英文 · 双语对照 · 无补充要求」，改设置时随之更新，运行中写「设置已锁定」），下面只留目标语言、源语言（PDF 为翻译方式）、输出（双语对照 / 仅译文分段按钮）和补充要求（旁边「编辑 ↗ 设置」），其余开关和输出位置收进默认收起的「高级」折叠，标题旁写「N 项 · 已按常用场景设好」；输出位置改为「源文件旁 / 自定义」分段按钮。所有输入框、下拉框、语言框和路径栏的悬停、聚焦外圈、选中底色、光标颜色和禁用样式统一为一套；可输入的下拉框聚焦时不再整段选中，光标放在末尾，直接打字即替换原值。

  **Interface refresh (step two): workspace layout**: The source row on the four translation pages becomes one path bar at the top (paste a path and press Enter to read it; Choose… on the right picks files or a folder; Read again appears once something has been read). With no source yet, a drop area fills the space below (large icon, supported extensions, Choose files and Choose folder), and reading shows skeleton placeholders. The stats bar always has four blocks plus one quiet note on the right (such as "Identical text is translated once"; on Excel with old .xls files it says the conversion is chosen at start), and fades in block by block after reading; a ready line below gives the number of items to translate and notes that translation memory is reused first and the scene and source language are detected at start. While running, one progress box shows the steps "Read files → Translate → Write files" for Excel, Word and PDF (plus a row-height step on Excel when it applies), a progress bar with a moving sheen, and the activity log in a collapsible section. When done, a result card sits above the list (green, amber or red, with Resume translation, Open output folder, View report and Send feedback on this task on the card), and the right column's button becomes Translate again. The right column is slimmer: a recipe summary on top (such as "Auto-detect → English · Bilingual · No extra instructions", updated as settings change and marked locked while running), then only target language, source language (translation method on PDF), output (a Bilingual / Translation only segmented control) and extra instructions (with Edit ↗ Settings); every other switch and the output location move into an Advanced fold, collapsed by default and labelled "N items · preset for common cases". Output location becomes a Next to source / Custom segmented control. Hover, focus ring, selection colour, caret colour and disabled look are now one shared set for every text field, dropdown, language picker and the path bar; type-to-search dropdowns no longer select their whole value on focus: the caret goes to the end and typing replaces the value.

- **界面焕新（第三步）：「翻译」入口页、拖入即分配、投放区的光**：打开应用先进入新的「翻译」页（侧栏「翻译」一项）：上面一块投放区，可以拖入文件或文件夹，也可以点「选择文件」「选择文件夹」，四个类型芯片（带支持的扩展名）直达各页；下面两栏，「最近」列出最近 4 个任务（点一下到任务页并选中），「已就绪」列出文档翻译模型、PDF 与图片翻译模型、记忆库条数和语言、补充要求、图纸转换工具的状态。把文件或文件夹拖到窗口任意位置，Translator 按扩展名数一数，进入文件最多的那个类型页并直接读取（文件夹里的 `~` 临时文件和翻译输出文件夹不算）；同时有其他类型的文件时提示一句「另有 N 个……，可到对应页面处理」；已经在某个类型页上、拖入的东西里有这一页认的文件时，留在当前页；那一页正在翻译时提示完成后再放入；没有可翻译的文件时提示一句，留在原处。入口页和各类型页空状态的投放区有新的光效：指针附近的一段边框亮起，底面一层很淡的光晕和点阵随指针移动，图标轻微倾斜；拖着文件经过时边框换成绕圈流动的多色边光，松手即停；系统开启「减少动态效果」时只亮不动。

  **Interface refresh (step three): Translate home page, drop to route, drop-area light**: The app now opens on a new Translate page (the Translate item in the sidebar). At the top is a drop area: drag in files or folders, or use Choose files / Choose folder; four type chips (with their supported extensions) go straight to each page. Below, Recent lists the last four tasks (click one to open it on the Tasks page), and Ready shows the document model, the PDF & image model, the translation memory's entry count and languages, extra instructions and the drawing converter. Dropping files or folders anywhere on the window counts them by extension and opens the page with the most matching files, reading them straight away (`~` temporary files and translation output folders inside a folder are skipped); if other types were included too, a note says which pages can take them. Already on a type page whose files are in the drop, the app stays on that page; if that page is translating, it asks you to drop again when it finishes; with nothing translatable, it says so and stays put. The drop areas on the home page and on each type page's empty state have a new light: the stretch of border nearest the pointer lights up, a faint glow and dot grid follow the pointer, and the icon tilts slightly; while dragging a file over it the border becomes a multicolour light that circles round, stopping on release. With the system's Reduce motion setting on, it lights up without moving.

- **界面焕新：反馈页两栏铺满**：反馈页左右两栏不再是两张卡片，直接铺满窗口，描述框和任务列表随窗口变高；任务列表改为一行一个任务（类型图标、文件名、类型 · 状态 · 日期），上方的搜索框随输入实时显示「匹配 N / M 条」；截图缩略图移到描述框下方。

  **Interface refresh: full-height feedback page**: The feedback page's two columns are no longer two cards and now fill the window, with the description box and task list growing with it. The task list shows one task per line (type icon, file name, type · status · date), and the search box above it shows "N of M" as you type. Screenshot thumbnails move below the description box.

- **结果卡上的「提示」说法**：翻译完成后，结果卡小结里原来写「N 处提示需手动处理」，Excel 的提示多是已自动修好的内容，绿色卡片上挂着「需手动处理」读起来像还有事要做。现在改为「N 条提示，详见报告」，各类型通用，颜色不变。

  **Wording of notes on the result card**: The result card summary used to say "N notes to handle manually"; on Excel most notes are things already fixed automatically, so the green card read as if work remained. It now says "N notes, see the report" on every type; the colour is unchanged.

## V10.2.0

- **优化：jsDelivr 卡住时「浏览扩展」不再久等**：扩展列表先取 jsDelivr，3 秒内没有结果就同时取 GitHub 原始地址，先拿到的为准（原来要等 jsDelivr 超时 20 秒才换）；从列表下载扩展包时，一个地址 15 秒没有新数据就换下一个（原来 60 秒）。

  **Faster extension list when jsDelivr stalls**: The extension list is requested from jsDelivr first; if nothing arrives within 3 seconds it is also requested from GitHub directly and the first answer wins (it used to wait for jsDelivr's 20-second timeout). Downloading an extension package moves to the next address after 15 seconds without data (was 60).

- **「扩展」页：添加、浏览、安装和卸载扩展**：「更多文件类型」一节下加入「添加扩展…」（选 .zip）、「从文件夹添加…」、「制作扩展」（步骤说明，链接到扩展模板的项目主页）和「分享扩展」（打开扩展列表的提交说明）；新增「浏览扩展」一节，列出扩展列表里审核过的扩展（名称、说明、文件类型、作者、版本、大小、许可证），可搜索、按文件类型筛选、刷新，每项按状态显示「安装」「更新」「已安装」「随应用提供」或「不兼容」（悬停看原因），并有「项目主页」；联网失败而用缓存时提示「列表可能不是最新（无法连接）」。安装前都先弹确认框：版本（更新时写出旧版本 → 新版本）、文件类型、许可证、来源或作者与审核情况、大小和告警，并写明「扩展由第三方作者编写，以你的用户权限运行，可以读写你的文件；只安装你信任的来源」；检查没通过时逐条列出原因，不提供安装。安装在后台进行，卡片上显示下载 / 安装进度；装好后新类型立即出现在侧栏（用户关过它的不动）并提示「已添加到侧栏，可在此页调整」。翻译类型列表里装进来的类型来源写「已安装 · 版本」，旁边有「卸载」（确认一次）；装坏了的扩展在「更多文件类型」里写明原因并可卸载。任务中心里已卸载类型的任务显示为「<名称>（扩展已卸载）」。后端消息补全英、法文译文。

  **Extensions page: add, browse, install and uninstall extensions**: The More file types section gains Add extension… (pick a .zip), Add from folder…, Build an extension (steps, with a link to the extension template's project page) and Share an extension (opens the extension list's submission guide). A new Browse extensions section lists the reviewed extensions from the extension list (name, summary, file types, author, version, size, license), with search, file-type filters and Refresh; each shows Install, Update, Installed, Bundled with the app or Incompatible (reason on hover) by state, plus Project page. When only the cached list could be read it says the list may be out of date. Every install asks first: version (old → new for an update), file types, license, origin or author and review status, size and warnings, and the notice that extensions are written by third-party authors, run with the user's permissions and can read and write their files. A package that fails the checks lists every reason and offers no install. Installs run in the background with download / install progress on the card; a new type appears in the sidebar right away (unless the user had hidden it) with the toast "Added to the sidebar". Installed types show "Installed · version" with an Uninstall button (asks once) in the types table; a broken install is listed with its reason and can be uninstalled. Tasks of an uninstalled type show as "<name> (extension uninstalled)" in the task center. Backend messages gain English and French translations.

- **文档：制作和分享扩展**：新增 docs/EXTENSIONS.md（写给扩展作者：扩展的组成、清单字段与三个函数、安装前检查的限制、制作 → 自测 → 试用 → 打标签 → 提交到扩展列表的步骤、用户那边的安装与卸载、命令行速查）；README 新增「扩展」一节，附扩展模板和扩展列表两个仓库的链接；docs/ARCHITECTURE.md 新增「安装与子进程宿主」「扩展列表」两节。

  **Docs: building and sharing extensions**: Adds docs/EXTENSIONS.md for extension authors (what an extension contains, manifest fields and the three functions, the limits checked before install, the steps from template to self-test, trial, tag and submission to the extension list, what install and uninstall do on the user's side, and a command reference). The README gains an Extensions section linking the extension template and extension list repositories, and docs/ARCHITECTURE.md gains the sections 「安装与子进程宿主」 and 「扩展列表」.

- **新增翻译类型「文本」（.txt、.md、.markdown），新增「扩展」页**：新增「文本」类型，翻译纯文本和 Markdown 文件（默认不在侧栏显示，在「扩展」页打开后出现在侧栏「翻译」组的最后）：标题、段落、列表项、引用、表格单元格和脚注逐项翻译，YAML 头、代码块、HTML 块、行内代码、链接地址、网址和邮箱原样保留，换行符、缩进和编码（UTF-8、UTF-16、GB18030）照原文件写回；「双语对照」默认关，开启后译文在原文下一行：段落和引用另起一段，列表项和脚注紧挨原文换行（原文行末加「\」），列表保持原来的紧凑；标题和表格单元格内为「原文 / 译文」；需人工处理的位置列在输出目录的「需复核清单.txt」和任务中心；模型把要翻的原文原样还回来（或译文没通过质量检查、退回原文）的，该处保留原文、记「未获得译文」，不算已翻译。它是随应用提供的第一个格式适配器（adapters/text/，只用标准库），页面「文本选项」旁的「?」显示清单里的说明。侧栏「资源」组新增「扩展」页（「插件」页不变）：一张表列出全部翻译类型，内置四种和扩展类型都可以开关「显示在侧栏」、上下调整顺序，至少留一种显示；内置四种默认显示，扩展类型默认不显示（settings.translation_types，schema v40；v39 里扩展类型调过的顺序升级时并入）；不显示的类型仍可从任务中心打开；列表下方新增「更多文件类型」说明：可以自行制作扩展、上传分享，也可以下载他人分享的扩展。「设置 → 翻译参数」的页签变为 Excel、Word、PDF、CAD 加每个扩展类型一个（用类型名，顺序同「扩展」页）：设置目标语言、源语言和提示词（/api/domains/ext:<id>），扩展类型的「内置规则」可以修改、恢复默认（extensions.<id>.prompt；默认取清单 adapter.json 新增的 prompt，没有则用通用的那份），速率与重试按文档翻译模型取默认值；翻译参数配置包带上 extensions 节（不含侧栏布局），导入时按类型逐项合并，单独导出的提示词文件只带各类型的语言和提示词。

  **New translation type Text (.txt, .md, .markdown) and a new Extensions page**: A new Text type translates plain text and Markdown files (not shown in the sidebar by default; switched on in the Extensions page, it appears at the end of the Translation group). Headings, paragraphs, list items, quotes, table cells and footnotes are translated one by one; YAML front matter, code blocks, HTML blocks, inline code, link addresses, URLs and e-mail addresses are kept as written, and line endings, indentation and encoding (UTF-8, UTF-16, GB18030) are written back as in the original. Bilingual is off by default; when on, each translation follows its source on the next line: as a paragraph of its own for paragraphs and quotes, and right below the source with no blank line for list items and footnotes (a trailing "\" on the source line), so lists stay as compact as in the original; headings and table cells read "source / translation". Places that need a person are listed in 需复核清单.txt in the output folder and in the task center; text the model hands back unchanged (or a translation sent back to the source by the quality check) keeps its source, is listed as not translated and does not count as translated. It is the first format adapter shipped with the app (adapters/text/, standard library only), and the "?" next to Text options shows the description from its manifest. A new Extensions page joins the Resources group (the Plugins page is unchanged): one table lists every translation type, and the four built-in types and extension types alike can be shown in or hidden from the sidebar and reordered, with at least one kept visible; built-in types are shown by default and extension types are not (settings.translation_types, schema v40; a v39 per-extension order is folded in on upgrade). Hidden types still open from the task center. Below the list, a "More file types" note explains that extensions can be built, uploaded to share, and downloaded from others. Settings > Translation options now has a tab for Excel, Word, PDF, CAD and each extension type (named after the type, in the Extensions page order): target and source languages and prompts (/api/domains/ext:<id>); an extension type's built-in rules can be edited or restored to the default (extensions.<id>.prompt; the default is the new prompt field in adapter.json, or the app's shared rules), and speed and retries use the translation model's defaults. The translation options bundle carries the extensions section (without sidebar layout) and merges it type by type on import; the separate prompt file carries only each type's languages and prompts.

- **CAD：译文不再伸出图框、越出表格格子或互相压字**：自动缩小超长译文时，除了相邻文字，也把原文四周的实线当作边界：图框内框、表格和图签的格线、房间的墙，用 LINE、多段线（LWPOLYLINE / POLYLINE，圆弧段不算）画的都认，含插入的图块里画的（按插入点、比例、旋转换算，图块互相插入不会死循环）；点划线、虚线（线型或所在图层的线型不是实线）不算。贴着图框的「说明」、表格里的长译文、图签里的工程名以前越线，现在按同一套档位缩字宽、字高，到最小比例仍放不下的照旧列入报告请复核。两个相邻文字都译长了（如左右相邻的房间名），各自只用两段原文之间空当的一半（一边用不完的另一边可以多占），不再各自伸进对方。simplex / romans 字体的西文改按逐字的真实笔画宽度估算（AutoCAD 实测约是原先统一估算的 1.3 倍），带重音的字母按底字母算。上下对照时新增的译文行留在原文的格子里，连 p、g 的下伸笔画也不压到格子的横线；格子里放不下时，原文在格子里往另一侧挪，腾出地方。日志、报告和设置说明里的「会压到旁边的文字」改为「会压到旁边的文字或伸出图框」。新增自测脚本 agent/testing/cad_layout_check.py：对比原图和译图，报出译图里新出现的压字、穿线，可画示意图。

  **CAD: translations stay inside the frame and table cells and no longer overlap each other**: Shrinking long translations now treats the solid lines around the source text as boundaries as well as neighbouring text: the frame's inner border, table and title-block grid lines and room walls, drawn as LINEs or polylines (LWPOLYLINE / POLYLINE; arc segments do not count), including ones inside inserted blocks (placed by insertion point, scale and rotation; blocks that insert each other do not loop). Dash-dot and dashed lines (by their own linetype or their layer's) do not count. Notes against the frame, long table entries and the project name in the title block used to cross those lines; they are now narrowed and reduced on the same scale steps, and text that still does not fit at the minimum scale is listed in the report for review as before. When two neighbouring texts both get long translations (such as room names side by side), each takes half of the space between the two originals (or more, if the other side does not need it) instead of running into the other. Latin text in simplex / romans is now measured with each glyph's real width (about 1.3× the old flat estimate in AutoCAD), with accented letters counted as their base letter. In source-and-translation output, the added line stays in the source's table cell, descenders of p and g included; when the cell is too tight, the source moves toward the other side of its cell to make room. Log, report and setting help text now say "overlap nearby text or run past the frame". New self-test script agent/testing/cad_layout_check.py compares the source and translated drawings, lists new overlaps and line crossings, and can draw a preview.

- **CAD：同一列说明的译文统一字高**：左端对齐、上下相邻、原字高相同的一列单行译文（逐条说明、备注），有的缩小了、有的没缩时，整列改成其中最小的字高和宽度比例，不再一列里大小不一；原文以冒号结尾的标题（「说明：」）不算进列。只会让字变小，不会压到别处。日志多一行「N 处译文与同一列的其他译文统一为相同字高」。另：折成两行多出文字实体后，表格格子缩字按重新数过的实体序号找对象（原先可能对不上）。

  **CAD: notes in one column share one text size**: When a column of single-line translations (left-aligned, stacked, with the same original height, such as numbered notes) has some lines reduced and others not, the whole column takes the smallest height and width factor, so sizes no longer vary within a column; a heading whose source ends with a colon (「说明：」) is not part of the column. This only makes text smaller and cannot create new overlaps. The log gains "N translations were set to the same text size as the rest of their column". Also, table-cell fitting now finds its entities by ordinals recounted after a line is split in two (they could be off before).

- **CAD：译文不压门的开启弧、箭头；双语逐行说明不压下一行**：缩字时除了格线，原文旁边的斜线和弧线（门的开启弧、入口箭头、多段线的圆弧段、轴号圈，含图块里画的）也挡住伸过去的译文：原文没碰到的线，译文伸过去会横穿时停在线前面（例：「门厅」→「Hall d'entrée」不再压着门弧，「主入口」→「Entrée principale」不再压着箭头）。上下对照时新增的译文行含 p、g、q、y 这类往下伸的字母，下面按字高留出下伸的深度，不再碰到下一行原文；放得下时多出来的空上下均分，不贴着任何一边；多行说明从上往下逐行排，每行按前面挪过之后的位置让开。版面体检脚本 agent/testing/cad_layout_check.py 也报斜线和弧线的穿线。

  **CAD: translations no longer cross door swings or arrows, and bilingual note lines no longer touch the next note**: Besides grid lines, diagonal lines and arcs next to the source text (door swings, entrance arrows, polyline arc segments, grid bubbles, including those inside blocks) now stop a translation that would run across a line the source did not touch (for example 「门厅」→ "Hall d'entrée" no longer covers the door swing, and 「主入口」→ "Entrée principale" no longer covers the arrow). In source-and-translation output, an added line with descending letters (p, g, q, y) keeps room for its descenders above the next source line; spare space is split evenly above and below; and multi-line notes are laid out top to bottom, each line making way for the lines already moved. The layout check script agent/testing/cad_layout_check.py now also reports crossings of diagonal lines and arcs.

- **CAD：窄格子里放不下的译文折成两行**：译文所在的格子（窄房间、图签格子等，四周有实线围着）里，缩到最小比例一行仍放不下的单行文字，在空格处分成宽度相近的两行，叠放在原处（先以原来那一行为中心，上下有文字时改为顶对齐或底对齐），两行都留在格子里、不碰上下的文字；格子上下也放不下的照旧列入复核。第二行是紧跟在后面的一段新文字（新句柄）；双语时两行都带新增译文行的标记，同一张图再翻一次时一起删掉。不在格子里的（「标签：数值」这类挨着摆的）不折。日志多一行「N 处译文缩到最小比例一行仍放不下，已在原处折成两行」，统计新增 layout_wrapped，写回后的文字数量核对把它算进去。

  **CAD: translations that do not fit a narrow cell are split into two lines**: When a single-line translation inside a cell (a narrow room, a title-block cell, anything enclosed by solid lines) still does not fit on one line at the minimum scale, it is split at a space into two lines of similar width stacked in its place (centred on the original line, or top- or bottom-aligned when text sits above or below), kept inside the cell and clear of text above and below; if the cell is too short for two lines, it is listed for review as before. The second line is a new text entity right after the first (with its own handle); in bilingual output both lines carry the added-line marker and are removed together when the drawing is translated again. Text that is not in a cell (such as a label next to its value) is not split. The log gains "N translations still did not fit on one line at the minimum size and were split into two lines in place", stats gain layout_wrapped, and the post-write text count check includes it.

- **修复：CAD 任务状态与文件状态说法不一、工作表名截在单词中间、补译看不到文档说明**：CAD 的每条问题（排版放不下、译文里仍有原文、乱码、未采用设定译法、未能翻译、暂不支持的对象）都带 severity=needs_review，任务中心的列表和详情徽章改说「需复核 N」，不再在文件写着「已生成 · 需复核」时说「已完成」；旧任务记录没有 severity 的，按所属文件的状态算待办。Excel 工作表名的翻译提示要求不超过 31 个字符；模型仍给长了，有空格分词的名称在词边界截断，并去掉截半的括号和挂在末尾的虚词（「Plomberie et évacuation des eau」→「Plomberie et évacuation」），中文名称仍按字符截。补译遗留原文（局部修正和整句重译）的请求末尾带上「自动识别场景」写的文档说明。

  **Fixes: CAD task status vs. file status, sheet names cut mid-word, leftover-text repair without the document brief**: Every CAD issue (text still too wide at the smallest size, leftover source text, garbled characters, preferred term not used, not translated, unsupported objects) now carries severity=needs_review, so the task center list and detail badge say Needs review N instead of Done while the file says Generated · needs review; older task records without severity count as open when their file needs review. The worksheet-name prompt asks for at most 31 characters; if a name is still too long, space-separated names are cut at a word boundary, dropping a half-cut bracket and a trailing function word (「Plomberie et évacuation des eau」 → 「Plomberie et évacuation」), while Chinese names are still cut by character. Leftover-text repair requests (local fix and full retranslation) now end with the document brief written by Auto-detect.

- **速率与重试按文件类型设置**：同时请求数、每批条数、每批字符上限、失败重试次数、单段重试次数从「模型服务」移到「设置 → 翻译参数」，Excel、Word、PDF、CAD 各一组（settings.rate，schema v38，core/surface_rate.py）；PDF 页签分「整页图像翻译」（同时生成页数、单页重试次数、新增同时审核页数）和「文字翻译」（自动组合中转 Word、原位替换和文字识别的页）两组。默认值按小参数模型调整：同时请求数云端 30（未解锁上限 20 → 50）、本地 3，PDF 同时生成页数和同时审核页数云端 10、本地 3，每批条数云端 20、本地 10（Word 8），每批字符上限云端 1000、本地 800，失败重试 2，单段重试 3。CAD 改按设置的每批条数和同时请求数（原固定 20 条），本地 Ollama 的同时请求数开始生效，PDF 转 Word 改用 PDF 文字那一组；Word 遇接口临时故障先整批重发再拆分，Excel、CAD、PDF 原位替换拆到最小仍缺译文的条目逐条重试。升级后速率与重试一律改用新默认值（含原按模型保存的速率，以及 PDF 同时生成页数、单页重试次数），改过的值只留在升级前自动备份的旧 settings.json 里；翻译参数配置包升到 v5，旧包照常导入，但其中的速率与重试不用。记忆库页右上角改为「设置」按钮（高级模式和三个开关，与「翻译参数 · 记忆库」是同一组设置），数值项只在「翻译参数」里。

  **Speed and retries per file type**: Parallel requests, items per batch, max characters per batch, retries on failure and retries per item move from Models to Settings > Translation options, one group each for Excel, Word, PDF and CAD (settings.rate, schema v38, core/surface_rate.py). The PDF tab has a Full-page image translation group (pages in parallel, retries per page, new pages reviewed in parallel) and a Text translation group (pages sent via Word, replaced in place or recognised in Automatic mix). Defaults suit small models: 30 parallel requests in the cloud (locked maximum 20 → 50) and 3 locally, 10 PDF pages generated and 10 reviewed in parallel in the cloud (3 locally), 20 items per batch in the cloud and 10 locally (Word 8), 1000 / 800 characters per batch, 2 retries on failure, 3 per item. CAD now follows its batch and concurrency settings (previously fixed at 20), local Ollama honours the parallel request count, and PDF via Word uses the PDF text group. Word resends a whole batch after a transient service error before splitting, and Excel, CAD and PDF in-place retry leftover items one by one. On upgrade, every speed and retry value takes the new default (including per-model speeds and the PDF pages-in-parallel and retries-per-page values); changed values are kept only in the backup of the old settings.json made before the upgrade. The translation options bundle is now v5; older bundles still import, without their speed and retry values. The top right of the translation memory page becomes a Settings button (advanced mode and the three switches, shared with Translation options > Translation memory); number settings live only in Translation options.

- **内部：翻译页面的类型描述与通用路径（界面和流程不变）**：为以后新增文件类型（格式适配器）做准备，本版不带任何新类型，四个翻译页面的界面、文案和流程与改动前逐项相同。新增 GET /api/surfaces，列出每个翻译页面的名称、文件类型、能力（能否暂停、续译等）、阶段和开关；设置新增 extensions，运行时注册的翻译类型各有一份语言、补充要求、开关和侧栏位置（schema v39，旧文件读入时自动补齐，不需要迁移）；CAD 和 PDF 原位替换改用共享的 core/text_translation_service.py（速率与重试、提示词、分批请求、记忆库，日志、统计和输出逐字不变）；api.surfaces.register_surface 可在运行时注册键为 ext:<id> 的类型，界面按描述符为它生成侧栏入口和工作区页面，任务中心按描述符显示名称、图标和暂停按钮。说明见 docs/ARCHITECTURE.md「类型描述符与通用路径」。

  **Internal: translation page descriptors and a generic path (no visible change)**: Groundwork for adding file types later as format adapters; this version ships no new type, and the four translation pages look and behave exactly as before. GET /api/surfaces lists each translation page's name, file types, capabilities (pause, resume and so on), phases and switches. Settings gain extensions: each type registered at run time keeps its own languages, extra instructions, switches and sidebar position (schema v39; older files are filled in on load, no migration). CAD and PDF in-place replacement now share core/text_translation_service.py (speed and retries, prompts, batched requests, translation memory), with identical logs, counts and output. api.surfaces.register_surface registers a type keyed ext:<id> at run time; the interface builds its sidebar entry and workspace page from the descriptor, and the task center shows its name, icon and pause button the same way. See the 「类型描述符与通用路径」 section of docs/ARCHITECTURE.md.

- **内部：格式适配器协议与通用执行器（界面不变）**：只需要「取字、写回」的文件格式以后写成格式适配器（adapters/<id>/ 下的 adapter.json 清单和 adapter.py 的 extract / write 函数），页面、模型调用、记忆库、重试、复核和进度都由应用负责。新增只依赖标准库的协议包 translator_adapter（ADAPTER_API_VERSION 1）；core/format_adapter.py 校验清单并加载适配器（不写 __pycache__），启动时注册 adapters/ 下随应用提供的适配器，每个成为一个 ext:<id> 翻译类型（两种输出都支持的自动带「双语对照」开关）；core/adapter_runner.py 是通用执行器：逐个文件取字、自动识别原文语言、文档说明、记忆库命中、分批翻译（受保护片段换成 ⟦n⟧ 标记）、残留原文体检、写回，需人工处理的单元写进输出目录的「需复核清单.txt」并在任务中心列出；速率与重试按文档翻译模型的吞吐量。说明见 docs/ARCHITECTURE.md「格式适配器」。

  **Internal: format adapter protocol and shared runner (no visible change)**: File formats that only need text taken out and translations written back will be format adapters (an adapter.json manifest and adapter.py with extract / write under adapters/<id>/); the app handles the page, model calls, translation memory, retries, review and progress. Adds the standard-library-only protocol package translator_adapter (ADAPTER_API_VERSION 1). core/format_adapter.py validates manifests and loads adapters (without writing __pycache__); adapters shipped in adapters/ are registered at startup, each as an ext:<id> translation type (with a Bilingual switch when it supports both outputs). core/adapter_runner.py is the shared runner: per file it extracts the text, detects the source language, writes a document brief, applies memory hits, translates in batches (protected fragments sent as ⟦n⟧ markers), checks for leftover source text and writes the file back; units that need a person go into 需复核清单.txt in the output folder and the task center. Speed and retries follow the translation model's throughput. See 「格式适配器」 in docs/ARCHITECTURE.md.

- **命令行：不开窗口翻译文件**：应用内的翻译引擎带子命令时是命令行（`translator-sidecar types | scan | translate | status | stop`，源码里 `python -m cli`）。Translator 开着时连上它，任务进任务中心；没开时在后台起引擎，命令结束后退出。类型和选项来自类型描述符，运行时注册的类型同样可用；没给的选项按应用里保存的值补齐（`apply_saved_defaults`，各页面的 `saved_request_defaults` / `saved_scan_defaults`）；`translate --dry-run` 只扫描和预检；退出码 0 完成、1 什么都没生成、2 参数或输入被拒、3 引擎不可用、4 需要确认（`--yes` 继续）。引擎启动后在应用数据目录写 `run/engine.json`（端口、令牌、进程号，仅本人可读，退出时删除）；可确认的三种预检拦截在 422 里带 `accept`。旧的 `scripts/translate_{excel,word,pdf}_cli.py` 和 `core/headless_*.py` 已删除，translator-direct 技能和跑批测试改用新命令行，测试模型写进隔离数据目录（`agent/testing/seed_test_model.py`）。说明见 docs/ARCHITECTURE.md「命令行与 MCP 入口」。

  **Command line: translate files without the window**: Given a subcommand, the app's translation engine acts as a command line (`translator-sidecar types | scan | translate | status | stop`, `python -m cli` from source). With Translator open it connects to it and tasks appear in the task center; otherwise it starts an engine in the background that exits with the command. Types and options come from the type descriptors, so types registered at run time work too; options left out take the app's saved values (`apply_saved_defaults`, each page's `saved_request_defaults` / `saved_scan_defaults`); `translate --dry-run` only scans and preflights. Exit codes: 0 done, 1 nothing produced, 2 bad arguments or refused input, 3 engine unavailable, 4 confirmation needed (`--yes` continues). The engine writes `run/engine.json` in the app data directory (port, token, process id; owner-only, removed on exit), and the three preflight stops that can be confirmed carry `accept` in their 422. The old `scripts/translate_{excel,word,pdf}_cli.py` and `core/headless_*.py` are removed; the translator-direct skill and smoke tests use the new command line, with the test model written into an isolated data directory (`agent/testing/seed_test_model.py`). See the 「命令行与 MCP 入口」 section of docs/ARCHITECTURE.md.

- **MCP：让智能体调用 Translator**：`translator-sidecar mcp` 是一个 stdio MCP 服务器（官方 `mcp` SDK 2.3，新依赖），工具与命令行一一对应：`list_types`、`scan_sources`、`start_translation`、`get_task`、`wait_task`、`stop_task`、`get_task_result`（输出文件、复核数与复核位置）。Translator 开着时任务进任务中心，没开时引擎随 MCP 会话启动和退出；需要确认的拦截返回给智能体，由用户决定后带 `accept` 再调用。冻结冒烟会拉起 `mcp` 握手并列出七个工具。

  **MCP: let agents use Translator**: `translator-sidecar mcp` is a stdio MCP server (official `mcp` SDK 2.3, a new dependency) whose tools mirror the command line: `list_types`, `scan_sources`, `start_translation`, `get_task`, `wait_task`, `stop_task`, `get_task_result` (output files, review count and review locations). With Translator open, tasks appear in its task center; otherwise the engine starts and stops with the MCP session. Stops that need confirmation are returned to the agent, which calls again with `accept` once the user agrees. The frozen smoke test starts `mcp`, shakes hands and lists the seven tools.

- **内部：从文件夹或 zip 添加扩展（后端，界面随后）**：core/adapter_install.py 检查第三方格式适配器并装进应用数据目录 plugins/adapters/<id>/<版本>-<随机>/，登记在同目录 installed.json（id、版本、目录、来源、SHA-256、安装时间、来源类型）。检查只拦明显越界，不是沙箱：只收 .py、.json、.md、.txt（样例放 samples/，只检查不安装），总体积 2 MB、200 个文件以内，拒绝原生库和可执行文件（按扩展名和文件头）、符号链接、指向压缩包外的路径、声明 requires 的清单，静态扫描网络、启动其他程序、加载原生或动态代码的写法；id 不能与随应用提供的类型相同，协议版本和最低应用版本须适用。新增 /api/extensions/preview（确认弹窗用的摘要，不改动任何东西）、/api/extensions/install（须 confirmed，并核对预览时的 SHA-256；后台安装，install-progress 轮询，可取消）、/api/extensions/installed、/api/extensions/{id}/uninstall。装好即注册为 ext:<id>（来源 installed），同一 id 再装时换成新目录、成功后删旧的；该类型还有任务在进行或已暂停时拒绝更新和卸载；卸载只删应用自己的拷贝，该类型的设置保留。启动时在随应用提供的类型之后注册已装的扩展，目录丢失或清单读不动的跳过；installed.json 损坏时备份为 installed.json.broken-<时间> 并按磁盘上的目录重建。任务快照里记下类型名称，卸载后任务中心仍能显示。

  **Internal: add an extension from a folder or zip (back end; interface to follow)**: core/adapter_install.py checks a third-party format adapter and installs it under plugins/adapters/<id>/<version>-<random>/ in the app data directory, recorded in installed.json next to it (id, version, folder, source, SHA-256, install time, origin). The checks stop obvious overreach and are not a sandbox: only .py, .json, .md and .txt files (samples go in samples/ and are checked, not installed), at most 2 MB and 200 files; native libraries and executables (by extension and file header), symbolic links, paths outside the archive and manifests declaring requires are refused, and a static scan refuses network access, starting other programs, and loading native or dynamic code. The id may not match a type shipped with the app, and the protocol version and minimum app version must fit. Adds /api/extensions/preview (the summary for the confirmation dialog; changes nothing), /api/extensions/install (requires confirmed and checks the previewed SHA-256; installs in the background, polled through install-progress, cancellable), /api/extensions/installed and /api/extensions/{id}/uninstall. An installed adapter is registered right away as ext:<id> (origin installed); installing the same id again switches to a new folder and removes the old one after success; updating or removing a type with a running or paused task is refused; uninstalling removes only the app's own copy and keeps the type's settings. At startup, installed extensions are registered after the shipped types, skipping any whose folder or manifest can no longer be read; a damaged installed.json is backed up as installed.json.broken-<time> and rebuilt from the folders on disk. Task snapshots keep the type's name so the task center can still show it after an uninstall.

- **内部：装进来的扩展在子进程里运行**：用户安装的格式适配器不再进翻译引擎进程，scan / extract / write 每次起一个子进程（translator-sidecar --adapter-host <目录> <动作>，core/adapter_subprocess_host.py），请求和结果只以 JSON 经 stdin / stdout 传递，适配器自己的输出和报错带「[扩展 名称]」前缀写进日志。扫描一个文件 30 秒、取字或写回一个文件 120 秒超时，到时结束子进程、该文件记为失败；适配器抛错或崩溃只影响这一个文件。子进程只拿到 PATH、语言等必需的环境变量，不含应用数据目录和任何密钥，工作目录为用后即删的临时目录。这是崩溃隔离，不是沙箱。随应用提供的类型仍在进程内运行。翻译引擎安装包收入完整的标准库（去掉图形界面、交互终端、开发工具、测试和链接 GPL 库的 readline / gdbm），第三方适配器 import 常用标准库不再随机失败；Linux 上解压后约多 2 MB。冻结冒烟新增一次子进程往返（取字、写回一份 CSV，并导入 21 个常用标准库模块）。

  **Internal: installed extensions run in a child process**: Format adapters installed by the user no longer load into the translation engine. Each scan / extract / write starts a child process (translator-sidecar --adapter-host <folder> <action>, core/adapter_subprocess_host.py) that exchanges only JSON over stdin and stdout; the adapter's own output and errors go to the log prefixed with 「[扩展 name]」. Scanning a file times out after 30 seconds, extracting or writing one after 120 seconds; the child is then stopped and that file counts as failed, and an exception or crash in the adapter affects only that file. The child gets only the environment it needs (PATH, locale), never the app data directory or any key, and runs in a temporary working folder deleted afterwards. This isolates crashes; it is not a sandbox. Types shipped with the app still run in process. The engine package now includes the full standard library (minus GUI, interactive terminal, developer tools, tests, and readline / gdbm, which link GPL libraries), so a third-party adapter importing a common standard-library module no longer fails at random; about 2 MB more unpacked on Linux. The frozen smoke test adds a child-process round trip (extract and write a CSV, importing 21 common standard-library modules).

- **内部：扩展列表客户端（后端，界面随后）**：core/adapter_registry.py 读取公开仓库 KlaraGraff/XL_Translator-extensions 由工作流生成的 index.json，先取 jsDelivr，失败换 raw.githubusercontent.com，缓存 24 小时（应用数据目录 cache/extensions-index.json）；两处都连不上时有缓存就返回缓存并标 stale，没有就返回空列表和一句原因，不报错。只列「官方」和「已审核」的条目，不合规的条目（缺三种语言的名称或简介、下载地址不是 https、SHA-256 或体积不合规）跳过并记日志，未知字段忽略。每个条目带状态：可安装、已安装、有更新、本应用装不了（协议版本或最低应用版本不符，附原因）、随应用提供。新增 GET /api/extensions/registry、POST /api/extensions/registry/refresh、POST /api/extensions/registry/install（须 confirmed；按条目的地址依次下载，大小和 SHA-256 不符就换下一个，都不符就不装；下载的包 id 和版本须与列表一致，再走与本地添加相同的检查；进度和取消沿用 install-progress / cancel-install；下载的 zip 装完即删），登记的来源类型为 registry。新装的扩展（本地添加或从列表安装，含命令行）设为显示在侧栏（扩展类型默认不显示）；用户在「扩展」页拨过它的开关时不动，更新和重装也不动。

  **Internal: extension list client (backend; the page comes later)**: core/adapter_registry.py reads index.json, generated by a workflow in the public repository KlaraGraff/XL_Translator-extensions, from jsDelivr first and raw.githubusercontent.com as the fallback, cached for 24 hours (cache/extensions-index.json in the app data folder). When neither is reachable it returns the cache marked stale, or an empty list with one sentence saying why; it never errors. Only official and reviewed entries are listed; an entry that does not validate (name or summary missing a language, a non-https download, a bad SHA-256 or size) is skipped and logged, and unknown fields are ignored. Each entry carries a state: available, installed, update, incompatible (API or minimum app version, with the reason), or bundled with the app. New GET /api/extensions/registry, POST /api/extensions/registry/refresh, POST /api/extensions/registry/install (needs confirmed; tries the entry's download addresses in order and moves on when size or SHA-256 differ, installing nothing if none match; the downloaded package's id and version must match the list before the same checks as a local package; progress and cancel through install-progress / cancel-install; the downloaded zip is deleted afterwards), recorded with origin registry. A newly installed extension (added locally or from the list, including from the command line) is set to show in the sidebar, since extension types are hidden by default; once the user has switched it on the Extensions page, neither an update nor a reinstall changes that.

- **命令行：制作和安装扩展**：`translator-sidecar adapter new <id>` 从模板生成一个能用的扩展（「键 = 值」文本文件的示例、两份样例、AGENTS.md、中英文 README、MIT 许可证、打包脚本和 Release 工作流；模板在 cli/adapter_template/，与模板仓库 KlaraGraff/XL_Translator-extension-template 逐字节相同，PR 检查会拉取模板仓库比对）。`adapter test <文件夹或 zip>` 做与安装时相同的检查，再在子进程里对 samples/ 中每份样例运行 scan、extract 和两种输出的 write，核对以原文作译文写回后再取字与原文一致、带标记的译文都写进了文件，问题和提醒逐条列出，通过时退出码 0、否则 1（`--json` 输出一个对象，`--skip-id-check` 用于检查随应用提供的类型）；扩展列表的自动检查跑的就是它。`adapter install <文件夹或 zip>` 经翻译引擎安装：先列出名称、版本、扩展名、许可证和确认提示，加 `--yes` 才装；`adapter uninstall <id>` 卸载并保留设置。`adapter pack` 先做静态检查，再用模板里的 scripts/pack.py 打出 `dist/<id>-<版本>.zip`（固定时间戳，同样的文件打出同样的 zip）、`.sha256` 和扩展列表的条目。冻结冒烟新增一次 new → test → pack。

  **Command line: make and install extensions**: `translator-sidecar adapter new <id>` creates a working extension from the template (a "key = value" text file example, two samples, AGENTS.md, Chinese and English READMEs, the MIT license, a pack script and a Release workflow; the template lives in cli/adapter_template/ and is byte-identical to the template repository KlaraGraff/XL_Translator-extension-template, which the PR checks clone and compare). `adapter test <folder or zip>` runs the same checks as an install, then runs scan, extract and write in both output modes on every sample in samples/ in a child process, checking that writing the source back as the translation gives the same text when extracted again and that marked translations all reach the file; it lists every problem and warning and exits with 0 when everything passes, 1 otherwise (`--json` prints one object; `--skip-id-check` is for checking a type shipped with the app). The extension list's checks run exactly this. `adapter install <folder or zip>` installs through the engine: it lists the name, version, file extensions, license and the confirmation notice, and installs only with `--yes`; `adapter uninstall <id>` removes it and keeps its settings. `adapter pack` runs the static checks, then uses the template's scripts/pack.py to build `dist/<id>-<version>.zip` (fixed timestamps, so the same files give the same zip), `.sha256` and the extension list entry. The frozen smoke test adds a new → test → pack run.

- **本文档统一用词**：「自动识别场景」开着且文档文字不少于 3000 字时，翻译前在本机找出反复出现的词，连同文档说明请记忆库清洗模型（失败时改用文档翻译模型）为每个词定一个译法，随各批一并发送；有批次没照用的随第一轮重译一次，不标需复核；多数位置采用了的译法按「自动保存」存进记忆库，备注写适用行业。适用于 Excel、Word（含 PDF 转 Word）和 CAD。真实模型复测：两份施工方案前后不一致的词由 13 个降到 3–7 个、11 个降到 2–3 个，请求数约多 15%–18%（#103）。

  **Consistent terms within a document**: With Detect scenario on and at least 3,000 characters of text, Translator finds recurring terms locally before translating and asks the memory cleanup model (or the document translation model if that fails) to fix one translation for each, sent with every batch; batches that did not follow it are retranslated once in the first round without being marked for review, and translations used in most places are saved to the translation memory under Auto save, noted with the industry. Applies to Excel, Word (including PDF to Word) and CAD (#103).

- **修复：文档翻译换成 DeepSeek / 阿里百炼 / 本地模型时存不进去**：新装状态下「PDF 与图片审核模型」默认跟随文档翻译模型，保存文档翻译时会连带校验它能否看图，不能就整单拒绝。现在保存只按能力检查正在改的那条连接；别的用途因此用不了时照常保存，在模型服务页标「不可用」并写明原因，保存后提示受影响的用途；通用设置保存、删除连接、导入配置也不再因此被拒。用到审核模型的 PDF 任务开始前给出同一句原因。顺带修复换服务商时推荐模型名没存下、刷新后显示「缺少模型名称」（#129）。

  **Fix: document translation could not be saved after switching to DeepSeek, Alibaba Bailian or a local model**: The PDF and image review model follows the document translation model by default, and saving used to reject everything when that follower could not read images. Saving now checks only the connection being edited; a use that becomes unavailable is still saved, marked Unavailable with the reason on the model services page, and a notice lists the affected uses. Also fixed: the suggested model name was not saved when switching provider (#129).

- **修复：「?」提示框闪烁跳位、网址伸出框外**：插件页安装进度刷新时，「?」提示框不再反复重建闪烁或跳到左上角，钉住的提示不再消失；提示正文两端对齐，网址单独成行、只在「/」后断行（#101）。

  **Fix: help tooltips flickering and URLs overflowing**: While install progress refreshes on the Plugins page, "?" tooltips no longer flicker or jump to the top-left, and pinned tooltips stay open; URLs sit on their own line and wrap only after "/" (#101).

- **修复：Windows 上装进来的扩展收发中文出错**：扩展子进程（`translator-sidecar --adapter-host`）的标准输入输出显式改为 UTF-8。冻结版不认 `PYTHONUTF8` / `PYTHONIOENCODING`，Windows 管道默认按系统代码页，中文路径会读错、带中文的结果写不出（V10.2.0 首次正式构建的冻结冒烟在 Windows 上因此失败）。

  **Fix: installed extensions failing on non-ASCII text on Windows**: The extension child process now reads and writes UTF-8 explicitly; the frozen engine ignores `PYTHONUTF8` / `PYTHONIOENCODING`, so on Windows its pipes used the system code page.

## V10.1.0

- **PDF 翻译方式「自动组合」（Beta）**：默认仍为整页图像翻译；自动组合按页分类（扫描页、可提取文字的页、图纸和大图）各选首选方式，另设保底方式；可提取文字的页经「PDF 处理组件」（pdf2docx 和 PyMuPDF，插件，不随应用打包）转 Word 翻译，再用本机 Word 或 LibreOffice 转回 PDF；图纸页原位替换文字、保留矢量（core/pdf_vector，目前只译成中文，可选 Tesseract）；按原页序合成一份 PDF，整份走 Word 时可同时输出 .docx；新增 /api/pdf/route-capabilities。「PDF 处理组件」在插件页「一键安装」（从 PyPI 或国内镜像下载固定版本，核对大小和 SHA-256，解压到应用数据目录并自检；macOS 约 94 MB，Windows 约 72 MB），也可「选择安装位置」连接自行安装的 pdf2docx；未安装时页数统计、扫描、预览和整页图像翻译照常，自动组合中原位替换和转 Word 的页改用保底方式，页面分类改用 pdfium。安装包不再包含 PyMuPDF（macOS DMG 回到约 50 MB）。插件页 GitHub 开源组件的按钮改为「项目主页」。「文字识别组件」（Tesseract OCR）同样可一键安装：程序和库取自 conda-forge 的固定版本（只取开头一段，不下载整个约 180 MB 的包），识别数据取自 tessdata_fast 4.1.0（英、法、德、西、意、葡），全部核对 SHA-256 并试识别一行字后启用，macOS 约 43 MB、Windows 约 51 MB；也可「选择安装位置」或自动找到系统里装好的。「图纸转换工具」（ODA File Converter，翻译 DWG 需要）也可一键安装：用户确认 ODA 的使用条款后，从 ODA 官网下载安装包（macOS 挂载 DMG 拷出 .app，Windows 用 msiexec /a 解包，不需要管理员），核对 ODA 的数字签名，并把一张小图纸转成 DWG 再转回 DXF 确认可用；按钮文字与另外两个组件统一为「选择安装位置」「重新检测」，一键安装的可卸载。PyMuPDF、pdf2docx 等沿用各自许可，见 THIRD_PARTY_NOTICES.md。

  **PDF translation method "Automatic mix" (Beta)**: Full-page image translation stays the default. Automatic mix routes each page by type (scanned, extractable text, drawings and large pages) to a preferred method with a safety-net fallback. Text pages go through the PDF processing component (pdf2docx and PyMuPDF, a plugin, not bundled), the Word pipeline, and back to PDF via local Word or LibreOffice; drawing pages have text replaced in place with vectors kept (core/pdf_vector, Chinese target only for now, optional Tesseract). Pages are reassembled in order, with an optional .docx when the whole file goes through Word. Adds /api/pdf/route-capabilities. The PDF processing component installs from the Plugins page with one click (pinned files from PyPI or a mirror, size and SHA-256 checked, unpacked into the app data directory and self-tested; about 94 MB on macOS, 72 MB on Windows), or a self-installed pdf2docx can be connected with Choose location. Without it, page counting, scanning, preview and full-page image translation work as before; in Automatic mix, in-place and Word pages take the fallback and page classification uses pdfium. The installer no longer contains PyMuPDF (macOS DMG back to about 50 MB). Plugin buttons for GitHub open-source components now read Project page. The text recognition component (Tesseract OCR) installs with one click too: program and libraries come from pinned conda-forge builds (only the first part of the roughly 180 MB package is read), recognition data from tessdata_fast 4.1.0 (English, French, German, Spanish, Italian, Portuguese); everything is SHA-256 checked and a test line is recognised before it is used (about 43 MB on macOS, 51 MB on Windows). Choosing a location or an existing system install still works. The drawing converter (ODA File Converter, needed for DWG) can be installed with one click as well: after the user accepts ODA's terms, the installer is downloaded from the ODA website (on macOS the DMG is mounted and the .app copied; on Windows msiexec /a unpacks it without admin rights), ODA's digital signature is checked, and a small drawing is converted to DWG and back before it is used. Its buttons now match the other components (Choose location, Check again), and a one-click install can be uninstalled. PyMuPDF and pdf2docx keep their own licenses (THIRD_PARTY_NOTICES.md).

- **记忆库词条备注与句中采用（Excel、Word、PDF、CAD）**：词条新增「备注」，填写后按场景选用；来源分「手动维护」「自动保存」，手动维护的译法在长句中同样注入并核对，未采用时整段重译（次数可设，0–5），仍未采用的标记复核；整段旧译文因词条修改而过期时改按句中词条重译并顶替旧条目；记忆库页面普通/高级模式，任务详情显示记忆库报告（settings schema v37，tm.db 自动升级）。除整页图像翻译外，PDF 转 Word、PDF 原位替换和 CAD 图纸走同一套规则（core.term_session.DocumentTermMemory）：CAD 未采用设定译法的在译文对照表标「需复核」，原位替换的列入该页复核清单，PDF 转 Word 沿用 Word 的标记；「只在…中生效」按原 PDF / 图纸路径判断。

  **Translation memory notes and in-sentence use (Excel, Word)**: Entries gain a note that limits them to the described scenario; sources are Manual and Auto-saved. Manual translations are injected and checked inside longer sentences too, with automatic re-translation (0–5 retries) and a review mark when still not followed. Whole-segment entries made stale by an edited term are re-translated by term and replaced. The library page has normal and advanced modes, and task details show a memory report (settings schema v37; tm.db upgrades automatically).

- **CAD**：任务中心「查看与修改译文」（译文对照表、Excel 导出导入、按修改写回，不调用模型）；补翻 ACAD_TABLE 表格并同步单元格原值和 TABLECONTENT，格子放不下按格高整段缩小；「输出图纸版本」默认跟随原图（早于 2000 存 2000）；缩字比例按目标语言分两套默认值（中文 100%/40%，其他 70%/40%），后端未传比例时同样按目标语言取值；双语不再重复与原文相同的编号、规格；MTEXT 段落代码保留，\L \O 后紧跟的西文单词不再被删。续译时不再把空的过程目录记录当成当前目录复制。

  **CAD**: "Review and edit translations" in the task center (translation table, Excel export/import, write-back without the model). ACAD_TABLE tables are translated with cell values and TABLECONTENT kept in sync, and text that overflows a cell is shrunk to the cell height. "Output drawing version" follows the original by default (older than 2000 saves as 2000). Shrink ratios default per target language (Chinese 100%/40%, others 70%/40%), also on the backend when a task omits them. Bilingual output no longer repeats codes and specifications identical to the source. MTEXT paragraph codes are preserved and a Latin word right after \L or \O is no longer dropped. Resuming no longer copies the current directory when a run recorded no work folder.

- **Word**：翻译文本框（含 VML 备份）和脚注、尾注；段内局部格式（加粗、斜体、链接、上标）用 <gN> 标记随原文发送并按标记写回，标记边界漏掉的空格补回；PDF 来源段落句中手动换行先并句；译过的固定行高表格行改为最小行高；译成中日韩时东亚语言设为译文语言；编号级别为空格式串时不再补出「1.」；「仅显示译文」保留文本框、图片和脚注编号（core/word_layout_fixes.py）。

  **Word**: Text boxes (with their VML fallback) and footnotes/endnotes are translated. Inline formatting (bold, italics, links, superscript) is sent as <gN> markup and written back by tag, restoring spaces lost at tag boundaries. Mid-sentence manual line breaks from PDF sources are joined; translated fixed-height table rows become at-least rows; the East Asian language is set to the target for CJK output; empty numbering formats no longer produce "1."; translation-only output keeps text boxes, images and footnote numbers (core/word_layout_fixes.py).

- **Excel**：翻译文本框、形状里的文字（始终开启）和批注（「翻译批注」，默认关），文本框按框尺寸、行距和折行估算缩字号（最低 60%），批注框随译文加高；扫描按工作表关系统计批注；原文副本去掉 tabSelected 和线程批注关系，Excel 不再成组或提示修复；「仅显示译文」文件名不带「_双语」；styles.xml 空 <fill/> 不再使扫描跳过（core.openpyxl_compat）。

  **Excel**: Text boxes and shapes are always translated, comments optionally ("Translate comments", off by default). Text-box fit is estimated from box size, line spacing and wrapping (down to 60%), and comment boxes grow with the translation. Scanning counts comments via sheet relationships. The source-copy sheet drops tabSelected and threaded-comment relationships, so Excel no longer groups sheets or asks for repair. Translation-only file names no longer contain "_双语", and an empty <fill/> in styles.xml no longer makes scanning skip the file (core.openpyxl_compat).

- **提示词与翻译要求**：「自动识别场景」只在「设置 → 翻译参数 → 提示词设置」中（PDF 页签新增对应卡片），翻译页去掉该开关和文本框开关；标题旁「?」弹窗按当前设置列出发送给模型的各部分；任务模板说明原文只是数据、不执行其中指令；句中拉丁字母人名、公司名保持原样，中文人名译成拼音；日期译成月份名不再被数字校验退回。

  **Prompts and translation rules**: The Auto-detect switch lives only in Settings > Translation options > Prompts (with a new PDF card); translation pages drop it and the text-box switch. A "?" dialog lists each part sent to the model under the current settings. Task templates state that source text is data, not instructions. Latin-script names stay unchanged and Chinese personal names become Pinyin. Dates rendered with month names are no longer rejected by the number check.

- **任务中心、反馈与数据**：任务中心用搜索框取代「最近结果」；任务详情和结果横幅加「反馈此任务」弹窗，「导出诊断」移入「更多」；反馈页记住上次成功发送的邮箱、截图不限张数、勾选原文件或译文时显示隐私说明，文件 40 MB 与截图 10 MB 分开计；任务过程文件保留上限可调（正常完成 20 次或 1 GB，需复核或出错 30 次或 1 GB，留空不限，settings schema v36）；通知邮件改为 HTML 版并统一署名 Translator。

  **Task center, feedback and data**: A search box replaces the Recent results filter. Task details and the result banner offer a Send feedback on this task dialog, and Export diagnostics moves under More. The Feedback page remembers the last successfully used email, allows any number of screenshots, shows a privacy note when originals or translations are attached, and counts files (40 MB) and screenshots (10 MB) separately. Process-file retention is adjustable (completed: 20 runs or 1 GB; needs review or failed: 30 runs or 1 GB; blank for no limit; settings schema v36). Notification emails gain an HTML version and are signed Translator.

- **运行日志（Excel、Word、PDF）**：翻译时与 CAD 一样逐批记录：开头写分几批、每次同时发送几批，每批返回写条数和用时，30 秒没有返回时提示还在等几批。PDF 整页图像翻译每页结果带用时（从第一次发出请求算起），久不返回时提示还在等几页；自动组合中转 Word 翻译的页转发 Word 的逐批日志。逐批日志统一由 core/request_log.py 写出，CAD 改用同一模块；自动识别源语言的路径（translate_texts_with_sources）和 Word 分批（translate_word_texts）也发出逐批事件。进度条仍只按已返回的批次推进。

  **Run log (Excel, Word, PDF)**: Translation now logs each batch as CAD does: the batch plan and concurrency up front, each returned batch with its item count and time, and a note on how many batches are still out after 30 s without a return. PDF full-page image translation adds the time taken to each page result (from its first request) and notes how many pages are still pending; text pages that Automatic mix sends through Word relay the Word batch lines. One module (core/request_log.py) writes these lines for every page, CAD included; the auto-language path and Word batching now emit per-batch events too. Progress still advances only on returned batches.

- **Excel 慢回答与超时**：180 秒时限改为每次请求各自计算（以前整批共用，重试、拆批、换连接只能用剩下的时间）；并发批次等第一批确认协议的时间不计入各自时限，第一批超时或被截断时其余批次不再跟着失败；重试后仍超时的批次拆成两半再试一次。「请求总时限已用尽；协议仍未确定」改为说明模型服务多少秒内没有返回；全部因超时失败时提示可用「仅补译未翻译内容」再翻。日志补记「模型返回的译文与原文相同」的条数。丢了空格的英文词句（如 Exhaustfan(withbackdraftdamper)）不再判为无需翻译，型号、单位、邮箱、文件名照旧跳过。

  **Excel slow responses and timeouts**: The 180 s limit now applies to each request on its own (it used to be shared by a whole batch, so retries, splits and connection switches only got what was left). Waiting for the first batch to settle the protocol no longer counts against concurrent batches, and a timed-out or truncated first batch no longer fails the others. A batch that still times out after its retry is split in two and tried once more. The "total request time used up; protocol still undetermined" message now says how many seconds the model service went without answering, and when everything failed by timeout the result suggests "Translate untranslated content only". The log counts translations returned identical to the source. Run-together English (e.g. Exhaustfan(withbackdraftdamper)) is no longer treated as needing no translation; model numbers, units, emails and file names are still skipped.

- **仅译文模式下「中文＋外文」整体翻译（Excel、Word）**：模型会把「6.1.1 土石方工程合计  Total of Earthworks」判成已有双语，中译法时那半句英文不是法文，仅译文模式下原样保留就等于没翻（反馈 FB-20261013-UVW95F）。现在这类格子或段落在仅译文模式下再按普通内容整体翻一遍，翻不出来的保留原文并标需复核；原文＋译文模式不变。真实模型复测时译文仍夹带原文外文（「Armatures Rebar HRB400 Ø12」「Total Total」），于是：混合语言提示词要求译文只用目标语言、原文里的普通外文词也要翻，仅输出译文时不再允许「已有双语」；译文里原样留着非目标语言片段时带提示重试一次，仍留着就照写译文并标需复核，日志和 Word 质量报告各记一条；原文里出现过的短语被写两遍时合并（Total Total → Total）；英文词表补 rebar、embedded、earthworks、formwork。

  **Mixed Chinese + other language in Translation only (Excel, Word)**: The model may treat "6.1.1 土石方工程合计  Total of Earthworks" as already bilingual, but for Chinese → French the English half is not French, so keeping it as is in Translation only mode left it untranslated (feedback FB-20261013-UVW95F). Such cells and paragraphs are now translated again as ordinary content in Translation only mode; anything that still fails keeps its source and is marked for review. Source + translation mode is unchanged. A real-model retest still left source-language words in the output ("Armatures Rebar HRB400 Ø12", "Total Total"), so: the mixed-language prompt now requires target-language-only output, including ordinary foreign words in the source, and Translation only no longer accepts "already bilingual"; when non-target-language fragments survive verbatim, the item is retried once with a hint, and if they remain the translation is written and marked for review, with a line in the log and the Word quality report; a source phrase written twice is merged (Total Total → Total); the English lexicon gains rebar, embedded, earthworks and formwork.

- **记忆库在全新数据目录上自动建表**：PDF 转 Word 和原位替换不经过任务开始时的 init_db()，在从没用过记忆库的数据目录上会报「no such table: tm_entries」；tm_manager 在本进程第一次打开某个库文件（或文件被删掉后）先建表。

  **Translation memory tables on a fresh data directory**: PDF-to-Word and in-place replacement skip the task-start init_db(), so a data directory that had never used the memory hit "no such table: tm_entries". tm_manager now creates the tables the first time a process opens a database file (or after it was deleted).

- **插件卸载先确认**：卸载一键安装的组件会删掉下载的文件，所以「卸载」先弹确认框；「断开」只忘掉选过的路径，不确认。

  **Confirm before uninstalling a plugin**: Uninstalling a one-click component deletes its downloaded files, so Uninstall now asks first; Disconnect only forgets the chosen path and does not ask.

- **提示词缓存命中**：发给模型的提示词改为内置规则 → 补充要求 → 返回格式要求 → 文档说明 → 本批词条、改正说明和格式标记规则（以前本批词条插在返回格式要求前面，用了记忆库时每批只有开头约三百字能命中缓存，现在同一文档各批共用约九成），提示词文字本身不变。按模型名认厂家（第三方平台同样适用）：Claude 给固定段打 cache_control，GPT 系列带 prompt_cache_key，Grok 带 x-grok-conv-id 请求头；DeepSeek、通义、智谱、MiniMax、Kimi、小米、混元官方自动缓存，不加标识。平台退回 400/422 时去掉标识重发一次，并随协议路线记住。从响应读用量，Excel、Word、CAD 每个翻译步骤结束时写「模型输入共 N tokens，其中缓存命中 M（x%）」，服务不报命中数时不写。提示词组成说明弹窗按新顺序排列。

  **Prompt cache hits**: Prompts now go out as built-in rules → extra requirements → output format → document notes → this batch's glossary entries, correction notes and tag rules (previously the batch entries came before the output format, so with the memory in use only the first ~300 characters of each batch could hit the cache; now batches of one document share about 90%). Prompt wording is unchanged. The vendor is recognised from the model name (also on third-party platforms): Claude gets cache_control on the fixed part, GPT models a prompt_cache_key, Grok an x-grok-conv-id header; DeepSeek, Qwen, Zhipu, MiniMax, Kimi, Xiaomi and Hunyuan cache automatically and get nothing extra. On a 400/422 the request is resent once without the marker, and that is remembered with the protocol route. Usage is read from responses, and Excel, Word and CAD log "N input tokens, M from cache (x%)" after each translation step when the service reports it. The prompt anatomy dialog follows the new order.

- **安装包瘦身**：macOS DMG 约 34 MB（V10.0.0 约 50 MB），Windows 安装程序约 31 MB（V10.0.0 约 32 MB），功能不变。sidecar 打包后用 ditto 放回（Tauri 复制资源会展开符号链接），DMG 改 ULMO（LZMA）；Tauri 外壳 release 开启 lto、codegen-units=1、opt-level="s"、strip；PyInstaller spec 排除 pygments、pytest、Pythonwin，不再打包 assets。

  **Smaller installers**: The macOS DMG is about 34 MB (V10.0.0: about 50 MB) and the Windows installer about 31 MB (V10.0.0: about 32 MB), with no change in features. The sidecar is put back with ditto after bundling (Tauri's resource copy expands symlinks) and the DMG uses ULMO (LZMA); the Tauri shell release build uses lto, codegen-units=1, opt-level="s" and strip; the PyInstaller spec excludes pygments, pytest and Pythonwin and no longer bundles assets.

## V10.0.0

- **应用内反馈**：侧栏新增「反馈」，可选择问题、建议或其他，填写描述和建议填写的邮箱（问题解决或功能上线时邮件通知），描述框可直接粘贴截图；附带内容按类别勾选，勾了哪些就上传哪些：系统与版本信息、任务日志默认勾选，原文件、译文默认不勾，日志、原文件和译文取自下方选中的一次或多次任务；另可添加其他文件，添加即上传（附件总计不超过 50 MB）；日志发送前把文件名替换为「文件1」「文件2」，可先查看将发送的全部内容。点击「发送」后底栏显示按实际已发出字节计算的上传进度，经中转站（feedback-relay/，Cloudflare Workers）存入私有仓库的 issue，附件 90 天后自动删除；无法发送时可保存为 ZIP。任务中心的失败任务可直接「反馈此问题」，帮助页的「反馈问题」改为打开此页面，不再跳转公开的 GitHub Issues。

  **In-app feedback**: A new Feedback page in the sidebar takes a problem, suggestion or other note with an optional contact. Screenshots can be pasted straight into the description. Attachments are ticked by kind and only ticked kinds are uploaded: system and version information and task logs are on by default, originals and translations are off. Logs, originals and translations come from one or more tasks chosen below; other files can be added and are uploaded as added (50 MB in total). File names in logs are replaced with placeholders before sending, and everything can be reviewed first. "Send" posts through a relay (feedback-relay/, Cloudflare Workers) into an issue in a private repository, and attachments are deleted after 90 days; when sending fails the package can be saved as a ZIP. Failed tasks in the task center offer "Report this problem", and the Help page's "Report a problem" opens this page instead of public GitHub Issues.

- **新增 CAD 图纸翻译**：支持 DWG 和 DXF，流程与 Excel、Word、PDF 一致，可暂停、停止和续译；DWG 需在新增的「插件」页配置 ODA File Converter；GBK 等中文编码和以 \M+ 转义保存中文的图纸可正确读写；自动识别时按文字实际语种判断是否需要翻译；macOS 上不再把正常图纸误判为损坏。扫描只列出图纸清单，DWG 在任务中转换并逐张写入运行日志（每张转换 3 次，原先 5 次），DWG 进度份额按转换耗时调整，多张图纸按文件大小分配；校验通过后删除中间 DXF 副本，「另存一份 DXF」默认关闭。

  **CAD drawing translation**: DWG and DXF drawings are supported with the same workflow as Excel, Word and PDF, including pause, stop and resume. DWG requires ODA File Converter on the new Plugins page. GBK and other Chinese encodings, and Chinese stored as \M+ escapes, are read and written correctly; auto-detect judges each text by its actual language; macOS no longer reports valid drawings as corrupt. Scanning only lists drawings; DWG files are converted inside the task with a log line per drawing (3 conversions per drawing instead of 5), DWG progress shares reflect conversion time and several drawings are weighted by file size. Intermediate DXF copies are deleted once a drawing passes verification, and "Also save a DXF" is off by default.

- **英语和法语界面**：「设置 → 外观与语言」可切换中文、英语和法语，页面、运行日志、提示和错误信息随之切换；输出文件夹名、报告文件和写入文档的复核标记仍为中文。默认主题改为浅色，原先「跟随系统」的设置在升级时改为浅色（settings schema v30）。切换语言时原地更新，不再重新加载页面或跳回 Excel 页。

  **English and French interface**: Settings > Appearance & language switches between Chinese, English and French; pages, the run log, notifications and error messages follow the selection. Output folder names, report files and review markers in documents stay Chinese. The default theme is now light; a stored "follow system" setting is migrated to light (settings schema v30). Switching language updates in place instead of reloading the page and jumping back to the Excel page.

- **配色与文案**：「设置 → 外观与语言」新增五套配色方案（素雅灰、暖调纸感、冷调柔光、青瓷、琥珀护眼），浅色和深色模式分别选择，默认素雅灰（settings schema v31）；界面、日志、报错和报告统一用语并改为正式书面说法，三种语言同步；「深度清洗」更名为「清洗记忆库」并在执行前提示费用。

  **Colors and wording**: Settings > Appearance & language offers five color schemes (Neutral grey, Warm paper, Soft cool, Celadon, Amber comfort), chosen separately for light and dark mode, Neutral grey by default (settings schema v31). Wording across the interface, logs, errors and reports is unified and made formal in all three languages. "Deep clean" is renamed "Clean up memory" and warns about model charges first.

- **提示词拆成内置规则和补充要求**：「设置 → 翻译参数 → 提示词设置」改为三部分：内置规则（只读，每次都发送，用户无法修改，避免改坏后拿不到译文）；「自动识别场景」开关（默认开，翻译前通读文档写一段文档说明附在内置规则后面），运行面板也可直接开关；用户的「补充要求」，可存多条、选一条用，只追加在内置规则后面，不替换。删除同步工程、资料管理、行政生活化三个预设（内容与内置规则重复）。旧数据自动迁移（settings schema v35，文档配置导出 v4）：原先选自动识别或任一预设的页面开启开关；改写过的预设、V9 的单条自定义提示词转成补充要求，原文一字不改；有内容被转换时，首次写入前先备份原设置文件到 backups/settings/。

  **Prompts split into built-in rules and extra instructions**: Settings > Translation > Prompt settings now has three parts: the built-in rules (read-only, always sent and not editable, so an edit can no longer break the translation reply); an "Auto-detect scenario" switch (on by default; the document is read first and a short description is appended after the built-in rules), also available in the run panel; and the user's extra instructions, several of which can be saved with one in use, always appended after the built-in rules, never replacing them. The Engineering, Records management and Administrative presets are removed (they repeated the built-in rules). Old data migrates automatically (settings schema v35, document config export v4): pages that used auto-detect or a preset get the switch on; edited presets and the V9 single custom prompt become extra instructions word for word; when anything is converted the original settings file is first backed up to backups/settings/.

- **停止与续译**：Excel 停止时保存已完成译文并标记「部分翻译」，可从中断处继续；手动停止不再记为接口错误，也不再产生额外模型调用；重新扫描不再误报「源文件已变化」。

  **Stop and resume**: A stopped Excel task saves completed translations, is marked as partly translated and can resume. A manual stop is no longer recorded as an API error or followed by extra model requests. Rescanning no longer falsely reports a changed source file.

- **密钥、限流与模型设置**：密钥无效或额度用尽时停止并说明原因；持续限流时先保存已完成部分。切换模型保留同时请求数，各连接独立保存速率，修正调整或删除连接后误报「尚未填写 API 密钥」。

  **Keys, rate limits and model settings**: A rejected key or exhausted quota stops the task with a reason; under sustained rate limiting completed work is saved first. The concurrency limit survives model switches, each connection keeps its own rate, and a false "API key not entered" warning after reordering or deleting connections is fixed.

- **翻译质量**：标准编号（如 GB/T 700-2006）不再送翻；自动识别时已是目标语言的内容保持不变；Word 不再插入非目标语言的译文，并提示文本框和脚注中未翻译的内容。

  **Translation quality**: Standard codes such as GB/T 700-2006 are skipped. With auto-detect, text already in the target language is left unchanged. Word no longer inserts wrong-language output and reports untranslated text in text boxes and footnotes.

- **性能与进度**：源语言为自动识别时 Excel 并发发送翻译请求；Excel、Word 多个文件的原文语言识别同时进行，「自动识别场景」的通读与语言识别同时进行；PDF 多个文件的文档说明同时请求，CAD 后续图纸的图纸说明提前同时请求；补译遗留原文按并发设置同时修复；Excel、Word 和 CAD 的进度按等待模型的时间分配，翻译后期不再停滞；PDF 进度平稳、页面列表实时更新。

  **Performance and progress**: Excel sends requests concurrently with auto-detect. Source-language detection for several Excel or Word files runs in parallel, and so does the auto-detect-scenario read-through alongside it; document descriptions for several PDFs are requested together, and those for later CAD drawings are requested ahead of time; leftover untranslated text is repaired in parallel. Excel, Word and CAD progress follows model wait time and no longer stalls late in translation. PDF progress is smoother and the page list updates live.

- **其他**：任务结束时保留匿名诊断记录，「导出诊断」恢复可用；Windows 关闭或更新时正在运行的任务正常收尾；提示消息移至顶部，窄窗口表格不再错位，未配置模型时引导至设置；自定义输出目录可通过「浏览」选择；任务进度事件在 macOS 上不再比运行日志慢一拍。

  **Other**: Each finished task keeps an anonymous diagnostic record and Export diagnostics works again. Running tasks end cleanly when the app closes or updates on Windows. Notifications appear at the top, tables keep their layout in narrow windows, and starting without a model opens the settings. The custom output folder can be chosen with Browse, and task progress no longer lags one step behind the run log on macOS.

## V9.5.9

- **任务历史更可靠**：损坏的历史记录会先备份再恢复，备份失败时保留原文件；历史暂时无法保存时，翻译仍可继续，结果会在本次运行中保留。

  **More reliable task history**: Corrupted history is backed up before recovery. If history cannot be written temporarily, translation continues and the result remains available in the current run.

- **记忆库切换更准确**：快速搜索、翻页和切换语言时，迟到的旧结果不会覆盖当前内容；“仅显示译文”提示也明确了独立“_原文”副本的控制方式。

  **More accurate memory-library switching**: Delayed results from an earlier search, page or language pair no longer replace the current view. The translation-only option now explains how the separate “_原文” copy is controlled.

- **PDF 单页操作与更新更安全**：单页重做或换回会计入活动操作，完成保存后才释放保护；更新检查失败不会误报为下载失败，也不会在无法确认活动任务时继续安装。

  **Safer PDF page actions and updates**: Single-page regeneration and restore count as active work until their results are saved. Update checks no longer call an activity-check failure a download failure or install while active work cannot be confirmed.

## V9.5.8

- **文件进度更准确**：Excel、Word 和 PDF 按每份文件的实际处理阶段显示“未开始”“待处理”“处理中”和“已生成”，不再笼统显示“排队中”；单文件 PDF 也会正确显示正在处理。

  **Accurate file progress**: Excel, Word and PDF now show each file as not started, pending, processing or generated according to its actual stage. Single-file PDFs also show processing correctly.

- **新一批不沿用旧状态**：重新扫描或更换来源后，旧的完成提示、文件状态和 PDF 页表会清空；快速切换时，较晚返回的旧结果不会覆盖当前内容。

  **Fresh state for a new batch**: Rescanning or changing the source clears the previous completion notice, file status and PDF page list. Delayed responses from an earlier selection no longer overwrite the current workspace.

- **历史任务打开正确工作区**：任务中心的“打开工作区”恢复所选任务自己的文件和结果；旧记录缺少原始来源路径或逐页记录时会明确说明。

  **Correct workspace for historical tasks**: Open Workspace restores the selected task's own files and results. Older records clearly indicate when the original source path or per-page records are unavailable.

## V9.5.7

- **同名文件不会互相覆盖**：同一批里若有同名的旧版和新版 Excel 或 Word 文件，译文会分别保存；不同文件译出的名称相同、或目标位置已有文件时，也会自动使用不同名称。

  **Distinct files keep distinct outputs**: Legacy and current Excel or Word files with the same name now produce separate results. Outputs also receive distinct names when translated filenames match or a destination file already exists.

- **Word 失败时不留下假译文**：Word 文件写入或成品检查失败时，不会留下看似完成、实际只有原文或内容不完整的译文文件；已有结果会保留。

  **No apparent Word result after a failed write**: If writing or checking a Word result fails, the app no longer leaves an apparently finished file containing only the source or incomplete content. An existing result is preserved.

## V9.5.6

- **Word 批次设置更简单**：默认每批最多 800 个字符，超过上限的长段自动拆分，内部每批最多 8 段。设置页不再显示段落数和拆分阈值两个多余选项。旧配置仍可读取，已有的字符上限设置会保留。

  **Simpler Word batch settings**: The default is 800 source characters per batch. Longer paragraphs split automatically, with at most eight segments per request. The redundant paragraph-count and split-threshold controls are removed. Existing settings remain readable, and a saved character budget is preserved.

- **重试默认值调整**：Word 单段严格重试默认 3 次，PDF 单页重试默认 1 次；PDF 页图并发的自动模式说明改为按图像模型吞吐设置决定。

  **Updated retry defaults**: Word strict retries default to three attempts and PDF page retries to one. The PDF page-concurrency hint now explains that automatic mode follows the image model's throughput settings.

## V9.5.5

- **工作表名称可翻译**：Excel 翻译默认将工作表名译成目标语言，并同步更新能确认安全的公式、定义名称、图表、下拉列表和内部跳转引用。遇到动态或无法确认的引用时，整份输出保留原工作表名并给出提示；原文副本仍保留原名。此选项与“公式显示值回填”相互独立。

  **Translated worksheet names**: Excel translation now translates worksheet names by default and updates supported references in formulas, defined names, charts, dropdowns, and internal links. If a dynamic or uncertain reference is found, the output keeps all original worksheet names and reports why. Original-copy sheets keep their names. This option is independent of formula display-value backfill.

- **可翻译输出文件名**：Excel、Word 和 PDF 均增加“翻译输出文件名”选项，默认关闭。开启后使用当前文本翻译模型生成目标语言文件名；原文件不改动，模型未给出可用译名时沿用原名。

  **Optional translated output filenames**: Excel, Word, and PDF now offer an output-filename translation option, off by default. When enabled, the current text translation model produces a target-language filename. The source file remains untouched, and an unusable name falls back to the original.

## V9.5.4

- **新增连接不再改动原连接**：在模型服务中新增连接并选择“跟随”时，只会让新连接借用来源主用连接的服务商、地址和密钥；原连接保持原样，新连接仍可单独选中和删除。

  **New connections no longer change existing ones**: Selecting Follow for a new connection borrows the source primary connection's service, address, and key for that row only. The existing connection stays intact, and the new row remains selectable and removable.

- **模型名称按连接保存**：每条跟随连接都有自己的模型名称，保存或切换连接后不会被另一条连接的名称替换。云端、本地和跟随三种方式各自保留原有模型配置，切回时恢复。

  **Model names stay with their connections**: Each following connection keeps its own model name when saved or reselected. Cloud, local, and following settings are retained separately and restored when switching back.

## V9.5.3

- **开源署名更新**：“更新与关于”中的署名与项目仓库中的许可文件统一显示为 OA，已有发布版本不受影响。

  **Updated open-source attribution**: The About page and repository license now show OA consistently. Previously published versions are unchanged.

- **更容易发现语言方向选错**：当表格内容与所选源语言明显不符时，任务结果会提示检查语言选择，不再只显示笼统的质量失败。

  **Clearer language-direction errors**: When a workbook clearly does not match the selected source language, the task result now points to the language choice instead of showing only a generic quality failure.

- **连接拥挤时自动恢复**：Word、Excel 和 PDF 翻译遇到本机连接资源不足时，会降低同时处理的请求数并重试；PDF 审阅失败也不必因此重新生成整页。

  **Automatic recovery under connection pressure**: Word, Excel, and PDF translation reduce simultaneous requests and retry when local connection resources run short. A PDF review retry no longer regenerates the whole page.

- **自动跟进同系列模型**：启动后会检查有无较新的兼容型号，通过当前连接验证后更新；如果新型号持续不可用，会恢复原型号并暂停反复尝试。当前覆盖指定的 OpenAI 和 DeepSeek 型号。

  **Automatic same-family model updates**: At startup, the app checks for newer compatible models and verifies them through the current connection before switching. If a new model keeps failing, it restores the previous one and pauses repeated attempts. Selected OpenAI and DeepSeek models are covered.

- **隐藏式解除数值限制**：在“模型服务”的“文档翻译”并发输入框输入解锁码并按 Enter，即可解除各翻译模块的批次与并发上限；页面不再单独显示解锁选项。

  **Discreet limit unlock**: Enter the unlock code in the Document Translation concurrency field under Model Services and press Enter to remove batch and concurrency limits across translation modules. The separate unlock control is no longer shown.

## V9.5.2

- **工作区显示“仅显示译文”开关**：9.5.1 已有该功能，但开关只出现在参数设置页，Excel 和 Word 工作区右侧的常用选项中没有显示。本版补齐工作区入口，打开后分别对当前 Excel 或 Word 任务输出译文。

  **“Translation only” in the workspace**: V9.5.1 included the feature in the parameter settings page, but the commonly used Excel and Word workspace panels did not show the switch. This release adds both workspace entries and applies the choice to the current document task.

## V9.5.1

- **连接更多文本服务**：以前部分兼容服务必须依赖特殊地址才能使用。现在程序会在同一服务和模型内识别两种常见接口格式，并在明确不兼容时有限切换，连接测试和实际翻译使用同一套判断。

  **More compatible text services**: Some compatible services previously required a special address. The app now recognizes two common interface formats for the same service and model, with limited fallback when incompatibility is clear. Connection tests and translations use the same logic.

- **OA 高并发设置**：输入 OA 解锁码后，批次数和并发数只需填写正整数，可以按服务能力使用更大的数值；未解锁时仍保持原有保护。

  **Higher OA limits**: After entering the OA unlock code, batch and concurrency settings accept any positive integer. The existing safeguards remain for locked settings.

- **可只输出译文**：Excel 和 Word 设置中新增“仅显示译文”。关闭时继续输出原文和译文，打开后结果文件只保留译文。

  **Translation-only output**: Excel and Word now offer “translation only”. The default keeps bilingual output; enabling it leaves only the translated text in the result.

- **模型名称自动更新**：更新配置时，旧的 `gpt-image-2` 会自动换成 `gpt-image-2.5-flare`，旧 DeepSeek 系列会换成 `deepseek-flash`，原有设置会继续保留。

  **Automatic model updates**: Configuration updates replace the retired `gpt-image-2` with `gpt-image-2.5-flare` and older DeepSeek model names with `deepseek-flash`, while preserving the rest of the settings.

- **默认行为保持不变**：没有打开新选项或输入解锁码时，原有双语输出和数值限制继续生效。

  **Defaults remain unchanged**: Without enabling the new option or entering the unlock code, bilingual output and the existing numeric limits remain in effect.

下载时请选择对应平台的安装包，并保留同名 `.sha256` 文件用于校验。本次构建的签名和公证状态以 Release 页面中的实际说明为准。

Download the installer for your platform and keep the matching `.sha256` file for verification. Check the Release page for the actual signing and notarization status of this build.

## V9.4.0
本版两条主线。一条是新能力「续译」：翻到一半被打断、或上次没翻完的文件，再次翻译时可以接着上次的成果补完，不再为已经翻好的内容重复付费。另一条是一次全仓体检：2026-08-29 把整个代码库从头到尾审了一遍，71 条发现里 67 条技术缺陷在本版全部修掉，其余产品取舍逐条拍板后随批落地。修复面很广，下面按「你会遇到什么」分组。

- 续译：同一批文件再次翻译时，扫描会认出同目录下上次的翻译产物，弹窗逐文件列出上次进度，问你「接着上次翻」还是「全部重新翻译」。选续译，PDF 只重跑上次没完成的页，Excel/Word 以上次的双语文档为底稿、只补没翻到的部分。续译前有资格核查：目标语言换了、源文件在上次翻译之后改过、页数对不上，一律判「整份重翻」并在弹窗里标明——绝不拿过期底稿静默丢内容。省下的是真金白银：按页、按量计费的接口调用不再为翻过的内容重复付费。
- PDF 单页重生成可以反悔了：重生成成功后，旧的那一页不再被删掉，留成紧邻的「上一版」；对比弹窗里能在「当前版／上一版」之间切换，看下来还是旧版好，点「换回这一版」即刻互换并重新装配输出文件——换回不调模型、不花钱。此前确认框里那句「覆盖之后取不回来」随之退役。重生成失败时，已交付的输出文件一个字节都不动（改为先写临时文件、成功才替换）；中止批量重跑时，排队没开跑的页不再被记成「图像生成失败」。
- 停止任务，已经付费拿到的译文不再陪葬：此前 Word 任务一停就整批放弃——停止前已经到手的译文既不写文档也不进记忆库，下次整批重新付费；Excel 编排层同样。现在停止只拦「还没发生的翻译」，已到手的照常写盘入库，没翻到的保留原文，停止前已检出的「需复核」条目也照常出现在结果里。停止后的去向全程可见：PDF 复核卡给没跑到的页单独出「已停止」计数，横幅报「还有 N 页没跑到」，重跑失败明说「旧产物已保留」。中断不再留孤儿临时文件。
- macOS 上退出应用不再「一刀切」：此前 Cmd+Q 对后台翻译进程是直接强杀，正在跑的任务连「已中断」都来不及记，临时目录也被留在盘上。现在退出改为通知它限时收尾（常态几十毫秒，最坏 25 秒）：在途请求收掉、任务历史落「已中断」、临时目录清干净。Windows 侧的同类改进已列入计划。
- Word 写入不再有机会破坏文档结构：一批藏在「表面看着正常、落到文档内部结构上不正常」层面的缺陷集中修掉——域（页码、交叉引用这类 Word 自动生成的内容）被当普通文本改写、带修订标记的删除文字没被算进原文、超链接和三段式页眉的制表符在替换时受损。替换模式（正文只保留译文）遇到含域的段落此前整段跳过、正文里留一段原文，现在原地替换：域结构原样保留，只把域外的普通文本换成译文，刷新域后页码、引用照常。自动目录不再被报成「需复核」的漏译——目录由 Word 按标题自动生成，翻了也会被刷新覆盖，现在改为一条提示，教你回 Word 按 F9 更新整个目录。
- Word 任务不再有「永远停在运行中」的挂死：恢复池里四条会让任务卡死的路径（停止落在重试间隙、线程异常吞掉计数、两处锁顺序死锁）全部堵死，停止和异常都能走到收尾、清掉临时文件。
- 「这条要不要翻」的判定修掉一批漏译和误伤：日译中／韩译中时「工事契約書」这类纯汉字标题此前被当成「已是中文」整条踢出流水线——不抽取、不翻译、报告里也没有；中译日时正确的日文译文反被日期单位检查打回中文原文；法语标准千分位「1 500」被数字校验判成丢了数字。补译模式的语言判定改掉一个根性偏差：证据词表只认识英法两种语言，其他语言「没话说」被当成了「确定不是」——现在拿不准一律按「未译」处理，多翻一条看得见、删得掉，漏译一条没人知道。韩语文档里纯汉字段落被误判成「已译韩文」的静默漏译同批堵上。
- 语言自动识别补齐 8 种文字系统：阿拉伯文、希伯来文、泰文、老挝文、缅甸文、高棉文、埃塞俄比亚文、谚文（韩文）此前在候选筛选规则里没有位置，这些语言的整份文件在「自动识别」下一个检测样本都产不出来，语言识别压根不发请求，整份原样输出。现在与拉丁、希腊、西里尔等文字同等对待。
- 模型返回格式异常不再污染译文：模型偶尔返回对象数组时，此前兜底逻辑把 `{'translation': 'Valve'}` 这类程序内部写法原样写进单元格、还存进记忆库；数组里缺失的元素被转成空串后既不写入也不计未译，报告显示全部成功。现在两类都按解析失败处理，交给重试机制。
- Excel 一批：补译对公式格的处理与全量翻译对齐，统一跟随「公式显示值回填」开关——开着（默认）时被译文命中的公式格按显示值覆盖成静态双语文本（原公式在「_原文」分表和原始文件里都完好），关着时公式格从计划到写入两层都不覆盖、公式源码也不再白白送翻；共享公式很多的大表不再随行数变慢得不成比例（此前 1600 行要 11.66 秒，上万行在界面上就是假死）；复核改判的单元格不再连译文带底色一起丢；.xls 兼容转换读值终于看类型——日期不再变成序列号、布尔值不再变成 1/0；`#N/A`、`#REF!` 这类错误值格不再被送进翻译管线，公式算出错误值的格子同样不进补译候选。
- .xls 老格式有了保公式的转换路线：装有 LibreOffice 的机器上，兼容转换交给它完成，公式、批注、多分表结构、合并单元格通常都能保留；没装或转换失败时退回原有的纯值兜底，且降级会以警告级别写进日志。弹窗和扫描告警按本机实际会走哪条路线给对应文案，先讲清「原始文件不会被改动」，再讲清各自的真实后果。
- 记忆库的写入从此「说实话」：手工新增／编辑词条不再一律弹「已保存」——被固定词条挡下、译文本来就一样、数据库出错，三种去向分开回报。清洗建议写入带并发校验：期间被你改过的词条不再被无声覆盖。清洗确认弹窗默认全不勾（此前默认全勾，一键写入就能拿机器建议盖掉人工校对），写入后逐行显示每条的去向（已写入／未改动／已跳过带原因），记忆库页顶新增常驻的「有 N 条清洗建议待复核」提示，建议面板顶部汇总本轮失效条数。
- 领域 Prompt 按「预设名＋目标语言」分开保存：此前一个预设只存一份自定义 Prompt，换目标语言后拿着写给别的语言的措辞继续翻，静默出错稿。现在同一预设下每种目标语言各存各的，恢复默认也只影响当前语言。设置页的内置领域 Prompt 改从后端取权威文本，不再是前端一份过期的硬编码副本。旧配置自动迁移；导出配置版本号升到 2，旧版应用读到会明确提示「请先升级」，而不是一段英文校验报错。
- 就地更新的两处如实相告：更新下载完成后的横幅改为「请尽快重启完成更新，期间新任务可能失败」——此前说「期间可以继续用」，而程序文件被换掉后新建任务确实可能报错；更新落地后第一次新建任务会弹一次拦截（取消／仍要开始／立即重启），每个版本只拦一次。手动检查更新失败不再画绿勾冒充「已是最新」；更新校验和下载不跟随跳转导致整次检查报废的问题一并修掉。
- 网络层两处「一颗老鼠屎坏一锅粥」：流式接口返回错误状态时，限流分类器自己先崩、把一次可重试的限流放大成整个任务失败；多密钥故障转移里单个连接构建失败被当成整个网关宕机，可用的候选一次都没试。都已修正。
- 密钥文件损坏可以自愈了：此前 keys.json 一损坏，保存新 Key 和清空全部 Key 都直接报错、没有出路。现在与设置文件同一套自愈方案：备份损坏原文、重建、记录恢复事件提醒你重新填 Key。「读不出」（权限、杀毒软件占用）与「解析不了」严格分开，前者拒写且盘上分毫不动。
- 界面与设置杂项：任务中心里已结束的任务不再被迟到的旧快照「复活」成运行中；PDF 重跑的进度轮询在后台服务重启时不再每 2 秒弹一条报错，改为逐步拉长间隔；右栏设置保存失败时界面回滚到磁盘上的真实值并提示，不再「界面改了、磁盘没存上」；设置页数值字段的上下限前后端对齐，越界不再报英文错误；主题切换先落盘成功才生效，失败回滚；复制输出路径失败时给出带路径原文的提示，不再静默；离线机器每次启动记一条更新检查失败、挤掉诊断里真实任务记录的问题修掉；补译评估里被跳过的内容（纯数字、命中忽略规则等）在任务日志里出一条分组汇总，「补译 N 处」的数字从此对得上总量。

## V9.3.4
本版处理的是 V9.3.3 发布当天收上来的几件事：一个让 Word 翻译在 macOS 上没法用的崩溃、更新卡片在小窗口里顶破屏幕、右栏类型选项「改了又变回去」，以及一轮默认选项的定稿。

- macOS 上 Word 翻译不再被「LibreOfficePython 意外退出」拦住：新版 LibreOffice 给自带的 Python 上了系统级的「启动限制」——只有 LibreOffice 自己能启动它，别的程序一启动就被 macOS 直接终止。此前程序每次任务都会真的去启动一次探路，于是桌面上必弹一个崩溃对话框，错误信息还劝你重装 LibreOffice（重装解决不了，限制是签在签名里的，装多少遍都在）。现在改为先读它的代码签名，读到启动限制就一次都不启动，直接改用内置方式处理自动编号，翻译照常进行，桌面上什么都不会弹。
- 检查更新的卡片不再顶破小窗口：展开完整更新说明后，卡片高度以窗口为限，说明文字在卡片内部滚动，底部的「更新」按钮始终露在外面。此前窗口不是全屏时，说明一长按钮就被顶出屏幕外。
- 右栏「本类型选项」不再「改了又变回去」：Excel 的「精调行高」和「锁定行高时缩字号」互为排他，此前切换时只有你点的那个开关写进了配置文件，被自动关掉的那个没写——下次启动一读配置，两个都是开的，界面看起来就像设置被默认值顶掉了。现在两个开关一起落盘；老配置里已经留下「两个都开」的，启动时按实际执行规则收敛（锁定行高优先），界面和实际行为保持一致。
- 各类型的默认选项定稿对齐：Excel 默认开「保留原文副本」「公式显示值回填」「锁定行高时缩字号」「标记需复核」，默认关「精调行高」；Word 默认开「本地自动编号预处理」「标记需复核」「保护封面和目录」，默认关「翻译页眉页脚」；PDF 默认开「生成压缩 PDF」「允许独立图片」「跳过 A3 及更大的页面」，默认关「逐页审核模型」。只影响没有改过对应选项的情况，你自己改过的选择不受影响。
- 右栏的「本次任务」折叠区改名「更多设置」，并默认展开：里面的专业领域和输出位置都是改了就一直生效的长期设置，叫「本次任务」会让人以为每次都要重设一遍。

## V9.3.3
本版围绕「文档里本来就有的译文」：什么时候该信它、什么时候该补一条、以及一个字都不许改的地方。起因是一份中→法实稿的页眉——项目名后面签的是合同上的法文法定译名，程序把整行重翻了一遍，那个译名被改写掉了。顺着这条线，「已有译文」的复核尺度、覆盖范围和 Excel 侧的缺席一并补上。

版本号定下之后又进来第二批，来自一轮跨语言实测和一次 PDF 结果页走查：一条会把整份非中文原稿判成「没有内容要翻」的闸门规则、Word 每篇白等 90 秒的转换启动、PDF 结果页会谎报「全部通过」的分组小结、批量重跑没有刹车，以及输出文件名的一次改版。

- 页眉页脚里已经有的外文，一个字符都不再被改写：一行页眉常常是「中文项目名 ＋ 合同上签的外文译名 ＋ 一段还没翻的中文」。此前这种行整条送去重翻，写回来的译名和合同上签的对不上。现在这类行走单独一条路：只补缺的那截，行里原有的内容原样保留，补出来的内容接在同一行里（页眉高度是版心定死的，另起一行会被裁掉）。判断依据是「这段文字是不是还没翻」，不是「它是不是目标语言」——目标语言选英文、合同上签的是法文译名，那个法文译名同样得留住。全译和补译两种模式走同一条路，同一份文档换个模式跑不会有两种结果。
- 「这一段旁边的文字真的是它的译文吗」，判定尺度整个换了一套：此前只要译文和原文的长度比正常就直接信任，于是「长度正常但讲的是另一件事」——也就是配错了对——根本到不了模型跟前，而这恰恰是最常见的一种错。现在长度比这条规则删掉了，判定改为看内容是否对应，且只抓三种硬伤：两段讲的明显是另一件事、原文里有整块内容在译文里完全没有、数字日期金额互相矛盾。单位和项目的法定译名、合同里的签署译名、术语选词不同、语序拆合不同、译文更简洁，全部照旧算「是它的译文」——这些译文多半是人写的，按机器译文的尺度去卡会把大批好好的人工译文判成不合格，然后在旁边再插一条机器译文。送判改成成批发出，单文件的复核上限从 200 对提到 2000 对。
- 复核范围补齐：Word 的表格单元格此前一律免检，Excel 整个补译模式此前完全没有复核。表格里恰恰最容易配错对——表头、单位名称、编号列，一格错位整列跟着错。现在两边都查，用的是同一套判定和同一份提示词。
- 判出问题的地方只加不删：程序能做的是补一条新译文接在后面，它没法替你判断原来那条是错配、是从别处复制过来的、还是压根不该在这儿。所以原有的原文和那条可疑译文一个字都不动，这一处在输出文件里标上「疑似原文异常」底色，报告里给出位置（Word 给段落/表格位置，Excel 给分表名和单元格坐标），你对着原件看一眼就知道该留哪条。
- 因为接口没返回结果而被从严处理的那些，不再上底色：接口抖一下就是整整一批（25 处）拿不到判定，给它们全涂上底色，你点开发现全是好好的译文——底色一失信，真正判出问题的那几处就跟着一起被跳过了。这一类只在报告里留一行记录。
- 「翻译页眉页脚」开关在补译模式下终于起作用：此前这条路上页眉页脚照样被抽出来、照样送模型翻译、照样计费，但写入时压根没有这一趟，译文直接丢掉。
- 检查更新时的下载进度条不再闪动和上蹿。
- 源语言不是中文时，整份文档不再被判成「没有要翻的内容」：`should_translate` 是全流程唯一的「这条要不要送去翻」闸门——Word 抽取与写入、Excel 词条收集与写入、补译分类全都经过它，它判 False 的内容不抽取、不写入、也不进任何报告，用户只看到任务跑完输出了一个原样的副本。目标语言不是中文时它走的是当年为「中文文档里夹英文」写的那条分支，最后一条规则是 `^[A-Za-z]+$` 且长度 > 3；产品现在支持 59×59 个语言对，这条规则套到别的文字上直接失效：泰语、高棉语、老挝语、缅甸语词与词之间不打空格，一整句话到这里就是「一个词」，正则一律不匹配；俄语 Договор、希腊语 Σύμβαση、日语假名、韩语谚文同样落空；德语 Straße、法语 Généralités 只因为一个变音字符就被判成不用翻。现在含非 ASCII 字母时按字母数放行（不含 ASCII 字母的谚文、假名、泰文短到 2 个字也是实词），仍含 ASCII 字母的短串（μm、Nº）继续按单位符号跳过，型号保护同步放宽字母类。放宽只落在非 ASCII 字母上，中译外这个主用法一个字符的行为都没变：DN、PE、Ltd、A3B12、DN100 照旧跳过；20 万条随机样本对照旧实现，收紧样本 0 条。
- 输出文件名从前缀改后缀，译文紧跟在原文件下面：`译文(中文)_X.pdf` 这种前缀会把同一份文档的原文和译文在访达里拆到两个字母段去。改成 `X_中文_高清.pdf`——`.`（0x2E）排在 `_`（0x5F）前面，`X.pdf` 就正好落在 `X_中文…` 的上一行。语言名本身已经隐含「这是译文」，不必再写「译文」二字，也顺手甩掉了全角括号这个跨平台/压缩包里的老麻烦。Excel 和 Word 的双语产物按同一条家族规则改成 `X_中文_双语.xlsx`（`{原名}_{语言}_{形态}`，「双语」和 PDF 的「高清/压缩」在同一个位置上——它回答的是「这份译文长什么样」）。旧目录重跑不会被当成没翻过：PDF 的跳过判定同时认新旧两种命名，Excel/Word 的重翻闸门本来就是按目录（`_翻译输出_`）判的，不看文件名。
- PDF 逐页表格改成按文件分组的审核面板，小结不再谎报：分组小结改成分桶累加（失败 / 建议复核 / 待处理 / 通过），不再下「全部通过」这种总结论，除非每一页都跑完且没问题。原先的写法把「还没跑完」算进「通过」，有两处会睁眼说瞎话——任务刚开跑时快照里所有页都是 pending，小结却写「17 页全部通过」；预处理就失败、页数为 0 的文件写「0 页全部通过」，还被标成干净分组，勾上「只看有问题的页」后整行消失，最该被看见的文件唯一看不见。折叠默认值也不再固化：原先第一次渲染就把默认值写进记录，而那一刻所有页都是 pending，全部分组被钉死成收起，「有失败的文件默认展开」在主流程里等于不存在。面板同时新增「只看有问题的页」筛选和「批量重跑失败页」。
- 批量重跑有「停止」了：一旦确认就没有任何入口能停——20 页失败的任务点下去，跑到第 2 页发现选错了也只能关掉应用，剩下 18 页每页 60~150 秒继续调接口。这是按页计费、覆盖不可回收的操作，缺这个口子等于没有刹车。后端没有取消接口，所以「停止」的真实含义是「不再发起后面的页」，已经发出去的那一页在服务端照跑到底——横幅按下之后如实改口说这件事，并且继续轮询到那一页收尾，不掐掉轮询（否则那一行会永远停在「重新生成中」）。顺带修掉两处会连撞 409 的守卫：循环守卫对任务切换是瞎的（`pdfBatchRerunState` 只在批量函数里改，切任务时压根不碰它），于是剩下几页背靠背连发、后端一路 409、连弹几条错误 toast 而一页也没重跑成；单页「重新生成」发起后到快照回来之前那一小段窗口里按钮仍是可点的，连点两下同样撞 409，现在发起即锁住整张表。
- Word 转换失败不再刷屏几十份崩溃报告，每篇文档少等 90 秒：macOS 26 会以代码签名为由直接杀掉 LibreOffice 自带的 Python（这是系统的启动约束，不是 LibreOffice 装坏了，换版本、重装都不会变）。原来的重试循环每秒试一次、连试 90 秒——那个循环本来是给「soffice 还没监听上端口」留的余地，那种失败下一秒就好；被信号杀掉却是重试一百次也不会变的状态，结果一次启动失败被放大成近 90 次崩溃，macOS 为每次生成一份报告、把「意外退出」对话框叠在应用窗口上，而这 90 秒买不到任何东西——最后照样退回纯 Python 兜底。现在：拉起 soffice 之前先探活一次（会挂的是它的 Python，先试它能省下 soffice 的启动时间，也不留空转的 headless 进程）；被信号杀（returncode < 0）直接抛错，不进重试循环；探活结论按解释器路径缓存，一批文档只探一次。另外 `_find_soffice` 的查找顺序反了过来：先看标准安装位置、再退回 PATH——PATH 上的 soffice 常常是包管理器生成的包装脚本（Homebrew 就是），它不在应用包里，拿它往上推 `../Resources/python` 推出来的目录根本不存在；而 `_find_libreoffice_python` 的「退回系统 python3」兜底整条删掉，通用解释器永远 import 不到 uno，拿它去跑桥接脚本换来的就是上面那 90 秒。实测同一份文档：90.4 秒 → 0.0 秒。
- 图片生成超时从 180 秒调到 240 秒，但「测试连接」不跟着涨：中转站按张收费、跟耗时无关，客户端超时不省钱，只是把已经付过钱的结果扔掉再原价重试。实测 156 次调用里有 16 次（10.3%）落在 180~240 秒之间被旧值误杀，折合 $0.48 白付；超过 240 秒的只有 1 次。所以超时是往上调不是往下调。测试连接单独定在 90 秒——它发的是现造的 64×64 白图，跟整页 A4 不是一个耗时量级，而且它是同步端点、界面转圈没有取消按钮，240×3 次等于 12 分钟干等。超时文案不再说「把并发调低」（两个延迟峰的并发均值是 8.76 对 8.77，跟并发无关），也不再断言「不是本机网络的问题」——这条规则全局共用，挂着不稳的 VPN 跑 Excel 批量时那句正好说反。
- 顶栏的模型徽章跟着当前页面走：它原来写死读翻译角色，站在 PDF 页也显示文本翻译的模型。现在按视图取角色——PDF / 图片页走 image 角色（实际拨的就是那一路），其余走 translation；角色表只缓存一份，切页面不重新发请求。任务中心的列表补上时间排序（运行中优先、同组内按创建时间倒序），此前压根没有排序、顺序由轮询返回顺序决定；打开时播种的选中项也改按当前筛选下的可见顺序取，不再选中一条列表里根本不显示的任务（左边没高亮、右边却在放别的内容）。
- 设置页删掉五个没有任何代码读的开关：`model_config_panel_open`（旧界面的面板展开状态）、`auto_pin_after_clean`、`retain_page_materials`、`enable_print_guard`、`enable_task_log`——全流程 grep 不到读取点，界面写进去也只是躺在 `settings.json` 里。其中 `enable_task_log` 是唯一有产品含义的：三个 runner 都硬写「开」，也就是任务日志永远开着；维持永远开——日志本身脱敏（不写源文、不写密钥、不写异常细节），界面清理里也有「日志」这一类随时能清，不值得为它在设置页多留一行。旧 `settings.json` 里带着这五个键照样读得进来，不会拒绝写入。
- 内部清理（不改变行为）：删掉界面重写后已无人调用的 `api_health` 模块、三个引擎里从来只传同一个值的死参数 `replace_prefix`、`word_task_runner` 里两个只为旧测试留的转发别名；新增一条测试守住「界面写进去的设置路径」和后端 `AppSettings` 的字段一一对得上。

## V9.3.2
本版做一件事：译文里残留未翻译的中文，从「只能报告」变成「先修、修不掉的才报告」。起因还是那份 81 页中→法实稿：报告里列出的残留，大半是「（四）」这类序号前缀和章节标题写法不一——它们有确定的改法，程序此前却只会把问题原样交给用户。九份实稿逐段回放（1904 个段落对）作为基线陪跑了整个改造，Word 和 Excel 从这版起共用同一套分类、修复与入库规则。

- 残留中文先分类、再各按各的办法处理：序号前缀（「（四）」写成「(IV)」还是「4.」）先看全文档在用哪种惯例，按惯例确定性改写，不花接口；「万」「亿」这类数量单位按设计放行，只在报告里记一句；夹在译文里的中文术语碎片和整段没翻的，才送去重译。此前不分青红皂白全归成一类「残留中文」，能机器改的和必须人看的混在一张清单里。
- 送重译的走两级阶梯：先「外科修补」——只把残留片段所在的句子重翻并回填，译文其余部分一个字不动，验收时整段长度不许暴涨（暴涨说明模型没听话、重写了全段）；修不好再整段重译，并把上一次不合格的具体原因写进提示词，模型不必再猜。阶梯有护栏：单文件最多修若干处、连续失败即熔断停手，两种情况都在报告里写明还剩多少处没修，不静默放弃。
- 翻译记忆库只存最终定稿：入库挪到全部修复之后，写入前还有三道卫生检查——序号前缀按本批惯例归一、节标题写法按多数派归一、带严重残留的词条直接拦下不入库并写明原因。此前修复前的半成品会进库，下次翻到相同内容，老问题被词库原样端回来。停止任务不再丢掉已经付费返回的词条；写库失败（比如词库正被占用）只降级为警告和一条报告条目，双语文件照常写出——此前 Word 侧一次写库失败会把整个任务带走，接口的钱花了，一个文件都拿不到。
- 章节标题的译法全文档对齐：「第X节」译成「Section N」还是「Nième section」，同一份文档里按多数派归一，离群的那几条改过来。词库清理页也新增同一规则的「惯例归一」建议：存量词条里写法不一的节标题生成确定性建议（不花接口），照旧是先给建议、你确认后才改。
- Word 表格单元格译文行数和原文对不齐时，不再清空原文重排：保留原文、译文追加在后面，并在报告里留痕说明。此前对不齐会把原文抹掉，出错时用户连对照的依据都没了。
- Word 的质量报告条目现在带原文位置锚点，从输出文档反查出来的条目明确标「输出」前缀；同一处残留写盘前后各查到一次的，只报一条，复核计数不再翻倍。

## V9.3.1
本版全部围绕「需复核」标注：此前一份 81 页中→法实稿里，输出文档上一处底色都没有——报告里的复核项，用户在文档里一条也定位不到；把标注补上之后又走到另一头，36 段全上了底色，其中大半是程序自己判过没问题的。这一版把标注收窄到真正需要人动手的两类（Word 和 Excel 同步），另外补上「已有译文的段落对」复核和两处会误导人的报告措辞。

- 报告里的每一条复核项，在输出文档里都能找到位置：`write_untranslated_docx`（「只补译未翻译内容」那条路）此前根本不接 `review_marks`，而 Word 实稿走的正是这条路——报告写着「正文段落 152 残留中文」，用户翻到那一页，纸面上和别处没有任何区别。补译写入器现在和双语写入器一样给命中的段落和单元格上底色；写入之后的体检（未译源文、译文残留中文）此前只写进报告、不落到文档上，现在也一并上色。同一份实稿重跑：输出文档里 36 段带上标注，此前是 0 段。
- 底色只留给真正要人看的两类：译文没出来（重试到底仍保留原文、译文里残留中文、未译源文）和原文本身可疑（原文疑似夹杂错误外文）。程序自己判过并放行的一律不再上底色——严格重试按恢复规则接受、语义仲裁判定与原文等义、混合语言路径语义接受，这三类只在质量报告里留一条记录。满篇底色等于没有底色：上一条改完，实稿里 18 条提示有 13 条是「已自动处理」，用户挨个点开发现都没事，下一次就整片跳过，真正要看的那几条跟着一起被跳过。同一份实稿重跑：带底色的段落从 36 段降到 4 段，与「需人工复核」清单一一对应。
- 「语义校验接受」这一类标记整类删除，Excel 同步收窄：常量、默认颜色、两张优先级表、设置页的色块全部拿掉，「复核三色」改叫「复核标记颜色」，只剩「保留原文复核」和「疑似原文异常」两格。留着一个不再涂的颜色不是没有代价——`_review_mark_highlight_values` 是「写完体检时认作我们自己涂的底色」的集合，它跟着默认色表走；用户要是把那格调成别的映射色，他自己在原文里涂过这个颜色的段落，即使整段没翻，体检也会直接跳过。Excel 侧的收窄改在唯一按内容判定的入口 `MixedLanguageResult.mark_kind` 上：仲裁放行的单元格不再上底色，因而也不再进那份叫「需复核」的定位清单——那个数字因此变准了。9.3.0 存下的配置里带着第三种颜色，读进来会被静默丢弃，用户自己调过的另外两个颜色原样保留，不需要手改配置文件。
- 「原文疑似夹杂错误外文」从「已自动处理」升为「需人工复核」：程序能做的只是把这段照常翻出来，它没法判断原文里那串外文是不是写错了——那要对着原件看。把它归进「已自动处理」等于替用户下了一个程序做不出的结论。
- 「已有译文」的段落对会被复核一遍再跳过：补译模式靠启发式判断某段中文的下一段是不是它的译文，判错的代价不对称——判成「已有译文」而其实没有，那段中文就永远留在文档里，报告也不会提（体检用的是同一套启发式），用户翻到才发现；反过来判成「未译」顶多多插一条译文，看得见、删得掉。现在被判为「已覆盖」的段落对会再审一遍，按代价从低到高：原文短且译文侧是外文简称（`N°`、`CCTEB`）→ 信任；记忆库里已有这段原文的译名且完全一致 → 信任；长度比落在正常区间 → 信任；剩下的才送模型仲裁，判不等义或拿不准一律打回重新翻译。整段中文后面跟着一个孤零零的 `II` 或 `PV` 是排版残留、不是它的译文，所以「译文侧是简称就信任」这条附带了原文也必须短的前提。单文件最多送 200 对进模型，超出的按原判处理并记一条日志。复核本身出错（限流、网络、模型异常）只退回原判，不连累整个文件——外层的按文件兜底会把它当成「打不开」，直接不出译文，那比不复核严重得多。
- 复核跑没跑、跑出了什么，每次都说：这两行日志此前只在「送了模型」或「真打回了」时才打印，于是一份干净文档上整个复核环节一声不吭，看起来像没做。现在只要有候选就报一句，哪怕结论是全都没问题；因为拿不到判定结果而从严打回的对数单列，接口出故障和译文真有问题不能长一个样。
- 译文残留中文的提示不再替用户猜原因：此前每条后面都固定跟一句「多为日期或编号」，而实稿里残留的是章节序号（一、二、三）和一个「函」字，一个日期都没有——这句话是在断言一个程序没有检查过的原因，会把人往错的方向引。现在只说实际残留了什么。
- 报告里的「问题片段」改叫「规则校验对不上的原文片段」：这些片段取自**原文**，是程序化规则校验拿去比对、没在译文里按字面对上的部分。原来的叫法配上一列中文日期，读起来像「译文里的日期错了」，而实际情况往往是译文把日期正常译成了外文写法、规则只是按字面没对上。
- 中文日期和数量单位会被译成目标语言写法，不再原样保留。
- 程序自己产出的双语内容不再被当成未译源文上报。
- 打开软件时会在顶部用一张卡片告知有新版本，并在原地走完下载、安装、重启：此前的检查每天只跑一次且缓存的是**结果**，9.3.0 发布前九小时跑过的那次检查，让当天每一次启动都静悄悄地什么都不显示。检查是一次条件 GET，取消节流；被节流的改成提醒本身（暂缓提醒、忽略某一版）。

## V9.3.0
本版只做一件事：PDF/图片任务跑完之后，可以单独把某一页重新生成一遍，输出文件和报告跟着改写。这件事以前整条封死——结果页恰恰是用户看出「第 12 页翻坏了」的地方，看出来了唯一的补救却是把整份文档重翻一遍，一页的问题付整份的钱和整份的时间。

- 终态任务能重新生成单页了：`api/task_manager.py` 的页操作入口此前两道闸——先判 `task.terminal` 直接 409（`reason="task_terminal"`），再判 `state != "paused"` 又一次 409。新增 `POST /api/tasks/{id}/pdf-pages/rerun` 专供终态：它不排队（`/pdf-pages/regenerate` 是「排队，继续翻译时生效」，暂停中的任务仍然走那条），在自己的线程上立刻重跑这一页，然后重新合成这个文件的输出 PDF、重写 `pdf_translation_manifest.json` 和 `pdf_translation_report.md`，并把刷新后的 `file_results` / `kpi` / `issues` 合回 `task.result`——任务中心的文件表和指标读的是这份 result，不刷新就会永远显示重生成之前的旧数。只换译文页图是不够的：输出 PDF 是页图拼出来的，报告里的失败占位页、有疑点仍采用、已生成页也都是按页统计的。
- 重生成期间任务始终留在终态：这不是「任务又跑起来了」。`task_manager.py` 里每一处「有没有任务在跑」都按 `terminal` 过滤（`list_tasks`、`active_task_count`、并发额度、忙碌连接、风险提示），翻回运行态等于让一次单页重跑占掉一个正式任务的名额。进行中的状态改放在 `ApiTask.rerun_active` 和 runner 的 `_rerun_page_key` 上，只经 `/pdf-pages` 暴露给界面（新增 `rerun` 和 `rerun_actionable` 两个字段）。终态任务能重跑的前提是 `_retire_terminal_task` 对 PDF 有意保留了真 runner（上限 12 个终态任务），逐页记录和归档目录都还在；runner 已经被淘汰时请求会给出明确的拒绝理由，不是静默失败。
- 重生成按正式任务的规矩预约共享 API 组：它真的要花接口用量。顺带在有 PDF/图片任务正在跑的时候自然给出 `surface_busy`，不用另写一套判断。任务开始时的租约在终态就释放了、调度器随之失效，所以 `ApiTask` 现在记下 `group_capacities` 供重跑再预约一次。模型配置在重跑时重新解析而不是沿用跑批时缓存的那份——常见情形正是「上次那一页失败是因为密钥填错了，改完再重跑」；顺带也让保留下来的终态 runner 不必一直攥着密钥。
- 逐页表格在任务结束后仍然渲染：`buildColLeft` 此前只在 active（有任务且未终态）的那半边渲染它，任务一到终态整块就消失，剩下的只有文件清单——表格入口没了，重生成的按钮再做也点不到。终态能做的只有单页重生成，「跳过该页」不给入口（它的含义是「接受这张失败占位页」，任务都结束了，接受与否没有下文）。
- 重生成前有一道危险操作确认：覆盖不可逆、要再花一次接口钱、还会改写可能已经交出去的输出文件，三条都写在弹窗里，主按钮是 `danger-solid` 的「覆盖并重新生成」。重生成期间只有那一行变成「重新生成中」、它的「查看对比」暂时关掉，别的页照常能看，不锁整屏。终态没有 SSE（事件流在终态就结束了），进度靠每 2 秒轮询一次 `/pdf-pages`，跑完自己停；轮询的起点放在 `fetchPdfPagesSnapshot` 里而不是发起处，切回页面接管一个正在重生成的任务也能自己接上进度。
- 两处配套：重跑期间的事件不再每条都整份重写任务历史文件（一次重跑几百行日志，原来是几百次 sanitize + 读 + 写），由收尾事件统一落盘；正在重跑的任务记录不许删除（`reason="page_rerun_active"`），否则重生成的输出会变成孤儿。

## V9.2.9
本版有两处是会改变输出内容的实质修复，不再只是显示层：一份 81 页中→法 Word 实稿暴露出多级编号被渲染成「一.1」并连锁污染了三个下游判定，以及「只补译未翻译内容」在源语言选「自动识别」时会产出一个字都没翻的文件。另外新增页眉页脚翻译开关，修好扫描报告弹窗关不掉、浏览只能选一个文件，并把两屏之间对不上的任务状态词收成一份。

- Word 多级编号按 Word 的规则渲染：文档的编号定义在 ilvl 1、2 上带 `<w:isLgl/>`（「所有层级用阿拉伯数字显示」），Word 显示的是 1.1 和 4.4.1，而程序按 ilvl 0 的 chineseCountingThousand 展开 `%1`，把「一.1」「三.2」「五.4.1」写进了 47 个标题——文档自带的目录缓存（1.1 抢工背景分析）就是 Word 实际显示什么的证据。`NumberingLevelDefinition` 增加 `legal_numbering`，legal 层级下所有占位符一律按十进制格式化，不看被引用层级的 numFmt。样例重跑：中文点号标题 0 个。
- 一点残留中文不再让整行译文被判成「未译」：`looks_like_target_text` 此前只要文本里有一个 CJK 字符就返回 False，于是带着上面那个（错误的）中文编号前缀的法语标题被判成「不是译文」——中文行因此被标为未译源文，再经 `looks_like_source_text` 把法语行也标成未译源文。同一份输出文档重新分类：未译条目从 51 降到 17。现在的判据是：目标语证据成立、CJK 只是零星夹带（≤12 字、≤20% 字母数、≥12 个字母）就算已译，正好覆盖编号前缀和日期片段，又不会把「夹了个英文术语的中文段落」误判成已译。
- 残留中文单独成一类、语气更轻：确实有十行法语里还带着中文日期（au 2026年8月9日）。它们此前被报成「输出文档仍存在未译源文」，名不副实。现在报「译文中残留少量中文」并点出具体片段，不计入未译数，也不进「仅补译」的待办清单。
- 表格题注会被翻译了：`_is_toc_or_field_paragraph` 此前只要段落里含任何域就返回 True，于是七条 SEQ 编号的表格题注（表 4.3-1 机械设备数量及操作人员）被静默跳过——既没翻译，报告里也没有，只有翻到那一页才会发现。现在只跳过整段结果由 Word 自己重新生成的域（TOC/INDEX/TOA/PAGE/PAGEREF 等）；其余的域，只有剥掉域结果后什么都不剩才跳过，所以带实词的 SEQ 题注会被翻。含域的段落强制走「插入到后面」而不是原地改写，原地重写 run 会把域抹掉。
- 页眉页脚可以选择翻不翻（新开关，默认关）：此前是硬性不翻，用户只能在报告里看到一句「不参与翻译」，一整页中文页眉留在输出里却无处说明。打开后页眉页脚走同一条抽取/翻译链路，但落笔方式不同——译文用「 / 」接在同一段原文后面，不像正文那样另起一行：页眉高度由节边距固定，多插一行会把版心顶下去或让文字被裁掉。按底层 part 去重（「链接到前一节」的页眉指向同一个 part，不去重会被追加多遍）；只有 PAGE / TOC 这类域的页脚仍旧跳过（域一刷新就覆盖译文）；重跑时原文后面已有译文的行不再追加；补译模式的覆盖率计划只看正文和表格，页眉页脚单独取一次并入待译集合。报告里的「翻译范围说明」跟着开关变，并且只在文档真的有页眉页脚文字时才出现。
- 「只补译未翻译内容」不再产出一个字都没翻的文件：源语言留在「自动识别」时，Excel 补译模式输出的文件和输入逐字节相同，日志写「待补 0，已覆盖 0」，而工作簿本身是一份没翻过的中文稿。语言预检在提取之后才跑，覆盖率计划拿到的源语言是字面量 `auto`，走进「非中文」分支，每个中文单元格都被判成「不是源文」，待译清单于是空了。覆盖率判定现在自己把「自动识别」解析成默认源语言，任何调用方都不可能再用这种方式把计划清零；Excel 那一路也像 Word 一样在开头就先解析。文件确实没什么可补时，日志会直说——那份输出按设计就是输入的副本，不该看起来像程序悄悄什么都没做。
- 自动识别下补译清单按识别出来的那门语言算：上一条兜住了中文稿，但自动识别的兜底是中文，一份法语稿的待补清单照样是空的——补译本来就是给「翻了一半的文件」用的，而这种文件恰恰常常是别人发来的外语稿。现在提取阶段先跑一遍普通提取，纯粹为了给语言预检提供样本，语言定下来之后再重建补译清单。代价是这个模式下每个工作簿多读一次；openpyxl 是只读方式读的，除了超大公式表之外都是零点几秒的事。
- 扫描报告弹窗关得掉了：这个弹窗没有高度上限，几十行就能把整个对话框撑得超出窗口，标题和「关闭」按钮一起被顶到屏幕外，看着就是卡死。现在正文在封顶的弹窗内部滚动，页头页脚按钮固定；长路径改为换行，不再被右边缘切掉。
- 「浏览 → 选择文件」可以一次选多个了：此前只接受一个文件。类型筛选该限制的是能选什么类型，不是能选几个。现在是多选，来源框显示这些文件共同的上级文件夹，扫描只覆盖实际选中的那几个文件。
- 同一个任务在两屏上不再有两种说法：一个已停止的任务，翻译界面写「已停止」，切到任务中心，列表徽标、详情标题、逐文件状态列全写「已中止」；另外三个终态同样各写各的，横幅下面的运行日志还写着「任务已中止」。状态词现在只有一份。两种写法冲突时取产品其它地方已经在用的那个词——按钮从头到尾叫「安全停止」，状态就只能是「已停止」。

## V9.2.8
上一版之后又把三种文档从选文件到看结果走了一遍，这次剩下的 6 处仍然是同一类：界面说的话和实际情况不符。文件夹里的图片被静默丢掉，逐页表格上方的说明和表格本身自相矛盾，按下停止后状态行几分钟一动不动，中止的任务在任务中心是一排零，同一次任务的页数在报告、清单、界面上是三个数。改动都在显示层，翻译流程没有动。

- 文件夹里的图片不再被静默丢掉：「允许独立图片」关着的时候，扫描到的 png/jpg 这些文件既不进任务清单、也不进「跳过项」，摘要上「独立图片 0 · 跳过项 0」和「这个文件夹里根本没有图片」长得一模一样。现在它们计入跳过项，扫描报告里逐个写明原因——是这个开关没打开，打开再扫一次就能翻。认得出是图片但本程序不接的格式（gif/heic/svg 等）也照此列出并写清可选的格式；直接选中单个图片文件时同理，不再报成含义完全不同的「不支持的文件类型」。
- 逐页表格上方不再说审核模型逐页检查过：「审核模型逐页检查版式与译文完整性」这句是写死的，逐页审核关着的时候它照样出现，而下面每一行写的都是「未审核 · 本次没有让审核模型看这一页」，一屏之内两句话对不上。现在这句话按本次任务有没有真的开审核来写。
- 按下「安全停止」之后状态行会自己往前走：停止后不再提交新页，但已经在飞的那几页要等模型接口返回或读超时，实测等过 4 分 10 秒。这句状态此前只在「检测到中止」和「又有页跑完了」两个时刻发出，而这段时间恰恰常常没有页再跑完——屏幕上四分钟一个字不动，看着像卡死。现在它每几秒重发一次，写明还在等几页、已经等了多久，并说清这几页在等什么、已生成的页面和素材都会保留。（在飞的请求没办法从中途掐断，所以没有「不等了，直接结束」这个按钮；能做的是把等待说清楚，而不是假装能结束它。）
- 中止的任务在任务中心不再是一排零：中止和失败这两条路直接跳过了逐页计数的刷新，任务中心里页级指标全是 0，而同一个任务在工作区写着「已生成 9 / 14 页」。现在这两条路也走一遍计数，并且给出「已生成页」和「未开始页」两格——跑完的任务不出这两格，那时已生成页数就是总页数，多一格只是占地方。
- 同一次任务的页数三处对齐：报告里的「页面完成进度」把只登记了失败占位、实际图像并不存在的页也算成已完成（那种页带着一个 `page_00N_failed.png` 路径，是占位图**将要**写到的位置，中止时这一步根本没跑）。实测同一次任务，报告写 12/14、界面写 9/14、清单里写 0。现在这些页不再算数，「已生成页面」的口径和界面上「已生成 N / M 页」一致——扣掉失败占位页和整页跳过的大幅面页，它们各自在报告里另有一行；未开始的页也单独占一行。
- 按了停止、页面却刚好全部跑完的任务会留下痕迹：这种任务照常走完成分支，小结只写「已完成 · 全部通过」，用户不知道自己按的那一下到底有没有截掉内容（此前只有运行日志里有一条）。现在小结会补一句「你请求停止时页面都已跑完，没有内容被截断」。

## V9.2.7
本版补 9.2.6 的账。上一版承诺「任务跑完，清单保留，状态列写『已生成』或『未生成』」——清单确实保留了，但状态列因为连接键对不上从未更新过，一直停在「未开始」。同一轮全量走查还查出界面凭空给结论、拦截没有出路、指标格空着等 13 处问题；修的过程中又查出 16 处同类的「界面说的话和实际情况不符」（运行中提前说「已生成」、页级问题被重复计数、中止的任务读不到页级数字、整份没翻的文件写着干净的「已生成」等）。29 处一并修掉。

- 文件清单的状态列真的会更新了：状态列此前用 `file_results[].source_path` 去和扫描项对应，而这个字段在隐私过滤里被置空，映射永远是空的——三种文档全中，9.2.6 的发布说明写了这条却没生效。现在两边统一按「去掉已知扩展名的文件名」对应（PDF 的 `name` 带扩展名、Excel/Word 不带），跑完写「已生成」「已生成 · N 处需复核」或「未生成」，失败原因写在同一行；同名不同后缀无法区分时宁可留在「未开始」，不蒙一个可能张冠李戴的结论。
- `合同.doc.pdf` 这类带两层扩展名的文件跑完也会更新状态列了：两边给的字符串本来就差一层扩展名（扫描项是主干名、逐文件结果是带扩展名的文件名），只削一次扫描项算出「合同」、结果算出「合同.doc」，键仍然对不上。现在两边都反复削到削不动为止；`报价.v2.pdf` 这种带点的主干名不受影响。
- 「已生成」不再提前出现在运行中的清单里：阶段 1 刚提取完文本的文件此前就被标成「已生成」，那会儿输出目录里一个文件都没有。运行中的那一格改成「已进入下一步」——它的真实含义只是阶段名里不再提这个文件。
- PDF 逐页表格不再凭空给结论：页还没生成、逐页审核也关着的时候，每一页都写着「待复核 · 版式一致，文本完整」。这句评价是界面在没有审核结果时自己兜底编出来的。现在没审核过的页就说没审核过。
- 审核模型上次测试失败时，拦截给得出出路：此前只闪一个约两秒的提示，提示里却写着「或明确确认继续」，而界面上没有任何地方能确认，看起来就像开始按钮坏了。现在改成留在屏幕上的弹窗，按钮是「取消」「去设置里重测」「不管测试结果，开始」（第三条只作用于本次任务，设置不会被改动），正文另外写清也可以先把逐页审核关掉再开始。后端为这类「有出路的拦截」补了稳定的 `reason` 字段，界面不再靠比对中文句子来认。
- 完成小结不再在有问题时承诺「全部通过」：这句话此前只看 `review.total_count`，而 PDF 那一路根本不填这个字段（它是 Excel / Word 的），于是一份「4 页全是占位页」的任务小结照样写「全部通过」，下面的清单同时写着「已生成 · 4 处需复核」。现在小结会说「N 页没能生成译文，已放失败占位页」（占位页是程序自己画的说明页，源页内容不在上面；说成「保留原页」会让用户去输出里找原文，真正原样保留的只有跳过的 A3+ 那一路）「N 页译文有疑点，已采用但建议复核」，只有一项都没有才说「全部通过」，有则说「其余全部通过」；问题页覆盖了全部页时连「其余」也不说——没有其余。逐文件那一格的口径同步对齐后端判 needs_review 的那套，占位页也算进「需复核」，不再出现「文件里有占位页、行上却干干净净写着已生成」。
- 页级问题的计数不再重复：`placeholder_page_count`、`review_failed_page_count`、`quality_flagged_page_count` 三个数两两都会落在同一页上（审核没通过的页有一半是直接退回占位页的，质检标记也留在退回前的最后一次尝试上），相加会把一页坏页说成两三页。后端新增与占位页严格互斥的 `suspect_adopted_page_count`（有疑点但仍然采用的页），小结只相加这两个互斥的数。
- 页级数字只算真的写出了文档的文件：一份 PDF 若所有页都生成失败，程序按设计不写输出文档（只留失败素材和报告，该文件计「未生成」），可它那几页占位图此前仍被算进小结的「N 页已放失败占位页」——用户按这个数去输出目录里数，永远差几页。占位页、有疑点的页、跳过的 A3+ 页、保护封面目录的段落这四项现在一律只统计已生成的文件；没生成的文件由「N 个文件没能生成」和逐文件行上的失败原因交代。
- 整份都因「跳过 A3 及更大的页面」而没翻的文件，行上不再只写「已生成」：这种文件确实写出了输出文档，但内容与源文件逐字节相同，一个字都没翻。后端把它判为不成功、并计入需复核，界面那一行却因为「有输出路径」就写了干干净净的「已生成」——同一屏上任务中心给它挂着「需复核」。现在写「已生成 · 整份未翻译」并带上原因，小结补一句「其中 N 个文件整份都是 A3+，一个字都没翻」，这种文件在场时也不再说「全部通过」。「其中 N 个」里的「其中」指的是已生成的那几个文件，所以这个数也只统计已生成的文件——否则一份整份 A3+ 又在合成输出那步失败的文件，会让横幅写出「1 个文件没能生成 · 其中 1 个文件整份都是 A3+」，而已生成的文件是 0 个。
- 「其余全部通过」不再漏算跳过的页：一份 4 页的文件里 2 页跳过、2 页占位，问题页其实已经盖满全部页，小结却还会补一句「其余全部通过」——并没有其余。
- 逐页表格能指出「有疑点」的是哪一页：本地质检（版式、文本比例等）跑在送审之前，审核判「通过」并不会清掉它的标记，而逐页表格只按审核结论显示，于是小结说「N 页译文有疑点」，表里每一页却都是绿色的「通过 · 版式一致，文本完整」，用户一页也找不到。现在质检疑点单独送到界面，这样的页显示「建议复核」并写出具体疑点。
- 已经采用了译文的页不再挂着「这一页没译文」那种深红：审核没通过但仍然采用的页此前和「生成失败」同色同调，而小结正把它算进「已采用但建议复核」。现在写「审核未通过 · 已采用」，用橙色的提醒色。
- 同一份记录不再因走的代码路径不同而给出不同页数：逐页计数原先在两处各写了一份，其中一份漏掉了质检标记页和跳过页。现在只保留一处实现。
- 任务中心不再把一页坏页数成两页：「失败占位」和「审核未通过」两格里有一半是同一批页（审核没通过的页有一半直接退回成占位页）。与「失败占位」并列的改成与它互斥的「有疑点仍采用」。
- 「有疑点仍采用」不算那些被丢掉的页：独立图片的失败分支把状态置成失败、却把「占位图」置成否（单张图片没有占位图这回事），于是一张连输出都没有的图会被算进「有疑点仍采用」——逐文件表写着「未生成」，同一屏的指标格橙着一句「有疑点仍采用 1」。这个数现在只认真正采用了译文的页。
- 中止和失败的任务也能看到逐文件结果与页级数字：终态契约里中止/失败把逐文件结果放在 `files`、正常完成放在 `file_results`，而界面几处统计只认后者，于是一个中途停下来的任务在清单和小结里都看不出哪几个文件已经写出来、有几页是占位。PDF 那一路的中止/失败消息此前也确实没带这两块数据（Excel、Word 一直带），现在补齐。
- PDF 运行状态行的页数不再被脱敏吃掉：「已完成 0 / 4 页」里孤立的斜杠被当成绝对路径，界面上出现「已完成 0 [path] 4 页」。
- PDF 任务在任务中心有指标了：页数、已生成、失败占位、审核未通过此前一个都不显示——PDF 的完成消息根本没填指标字段，界面上那段代码是死的。没发生的事不占格子：没开审核就不显示审核未通过。
- 压缩版 PDF 出现在产物表里：日志说生成了高清和压缩两份，产物表只列高清那份。产物路径白名单里漏了 `compressed_output`，这一项被脱敏成字面量 `[path]`，而界面把这个占位符当空值——就算加了那一行也永远是空的，两处一起改才生效。
- Excel 任务详情补上「需复核」一格，并且任务结束后不再留一句「未开始 0」。
- 结果定位清单在没有原文摘录时不再空占一列（Excel 的复核项不带摘录）。
- 运行日志里的「输出目录」写出目录名，不再是一句没有信息的「[path]」。
- 帮助页两处说法改成当前实际行为：应用内更新是下载、验签、就地替换（不是「打开 GitHub 的 DMG」，Windows 上也不是 DMG）；限流那段不再把 HTTP 状态码 429 摆给用户。

## V9.2.6
本版把三种文档从选文件到看结果的每一屏都走了一遍，改的是软件说给人听的那些话——它此前会把一次什么都没产出的任务说成「已完成」，会把同一个问题数成两个，也会把内部代号直接摆在界面上。

- 一个文件都没生成的任务不再说「已完成」：以前顶栏和结果横幅都写「已完成」，只有翻到详情才看得出产物为零。现在这种任务明确说是「已完成但有问题」，并写清有几个文件没生成。任务层面才用「成功」，单个文件用「已生成」和「未生成」。
- 任务跑完，文件清单和它的状态列还在：此前一收尾清单就被清空，哪个文件失败了、要重跑哪几个，只能凭记忆。现在清单保留，状态列写「已生成」或「未生成」，失败原因写在同一行。
- 「需复核」按文档里的位置计数，不再按判断次数：同一段话如果被判了两个问题，以前横幅说 5 处、清单里却排出 10 行。现在一处就是一行，两句判断合并写在这一行的说明里；「已处理」和「待复核」落在同一处时仍然分开列，不会把待复核的涂成绿色。
- 结果定位清单说人话：位置从 `body.paragraph[4]` 改成「正文 第 4 段」，「处理」列不再是空的，Excel 的复核类型和处理方式也都是中文说明。
- 文件表格里不再出现 `succeeded` 和 `[path]`：状态是中文徽标，产物列显示文件名，完整路径悬停可见——此前 Word 的产物路径会被整条替换成 `[path]` 这个占位符。
- 模型、连接、速度这几行去掉内部代号：不再显示 `translate_engine`、`profile_key` 这类只有开发者认得的键名，也不再把服务商和地址重复写两遍。
- 用户能看到的报错都收成一句中文：此前有些位置直接把接口返回的原始报文摆上来，一整屏英文加花括号。原始报文仍然完整记在运行日志和诊断包里。
- 中途换过连接会说出来：配了多个连接、主用连接失败自动切走时，结果里会写明后半程是哪条连接完成的，此前这件事只在日志里，界面上完全看不出来。
- 服务端限流不再刷屏：同一轮限流以前会连着刷几十行日志，现在收成两行——发生了什么、要你做什么，并且不再抛出 `120s`、`40` 这类只对程序有意义的数字。
- 设置页的连接状态会写明为什么测不通：以前只有一个红色「不可用」，原因藏在悬停提示里；模型列表是空的也会说清是没获取过还是配置改过了。
- macOS 上 Word 的编号处理不再报错：那一步在 macOS 上本来就做不了，此前却按失败处理并留下一条报错。
- 单元格批注会被数进去并在结果里说明：此前批注照常翻译，但结果里既不计数也不提。
- 输出目录名改短，不再长到把结果横幅上的按钮挤到换行。
- 其余界面修正：语言列表最后一行不再被裁掉；`.xls` 的提示先给不丢内容的那个选项；并行任务的提示改成人话；等待超过 20 秒会说明还在等什么；任务结束后停止确认框会自己关掉；进度条不再提前走到 100%；PDF 的几个计数口径统一；新建的任务立刻出现在任务中心，不用等下一轮巡检。

## V9.2.5
- 「导出（含 Key）」的文件里密钥不再是明文：以前导出的文件用记事本打开就能看见 API 密钥，而这种文件的用途恰恰是发给同事——微信、邮件、U 盘，路上被谁看到、被转发到哪里，发送方无从知晓。现在密钥段用软件内置的密钥加密，导出导入仍然是原来那两步，不配对、不输口令、不联网。安全边界要说清楚：任何装了本软件的人都能解开任何一份导出文件，它防的是文件在传输途中落到不相干的人手里，防不了拿到软件的人；确认弹窗里因此保留一句明确的警示，提醒不要发到公开群或网盘。
- 导出时可以选这份文件的有效期：7 天 / 30 天（默认）/ 90 天 / 长期有效。过期的文件对方导入时会被挡下，重新导一份给他即可。有效期只锁密钥，不锁配置本身——配置本就是明文，拦下来零收益。
- 文件被改动过就导不进去：加密时把配置正文的摘要一并锁了进去，中途只要改过一个字节——比如把某条连接的服务地址换成别人的服务器——对方导入时会直接报错，不会把改过的地址连同密钥一起装进他的软件。
- 导出文件扩展名改为 `.xltcfg`，以前导出的 `.json` 照常还能导入。
- 导入侧对四种异常分别给出说法：文件已过期、对方软件版本太旧解不开、这是旧版导出的明文文件（仍可导入，但会说明）、文件损坏或被改过。
- 旧版本软件拿到新文件不会出错：新文件里的密钥是整段拿掉之后再密封的，不留占位符，所以 9.2.4 及更早的版本会把它当成一份「不含 Key 的配置」正常导入，而不是写进去一串无效密钥。
- 内置的加密密钥不写进源码，发版时才从 CI 的加密变量注入，并在打包前校验它确实不是仓库里那把公开的开发兜底密钥——漏注入会让发布直接失败，而不是静默地发出一个「加密了但谁都能解」的版本。

## V9.2.4
- 修复所有导出都不落盘：点「导出」会弹一句「已导出」，但保存框从来没出现过，磁盘上也找不到文件。桌面端的网页容器在没有下载处理器时会把网页那套下载动作整个丢掉，成功提示却照旧弹出。现在导出走系统原生的保存框，由应用自己写盘（先写临时文件再整体改名，中途断电不会留下半截文件），写完才提示，并且直接报出保存到了哪里；在保存框里点取消不再有任何提示。涉及模型配置、文档翻译配置、诊断包、翻译记忆库的导出，以及任务结果的下载。
- 记忆库导出的 CSV 加上 BOM：此前中文 Windows 的 Excel 会把它按本地编码打开，整份表格全是乱码；导入时也会先剥掉 BOM，避免第一列列名对不上。
- 速率设置（批次大小、并发数）并入模型配置的保存方式：离开输入框即保存，「保存速率」按钮删除。填了超出范围的值，会自动改成允许的边界值并把输入框里的数字一并改过来，不再出现「界面上写着 999、实际存的是 32」。
- 图像生成模型不再参与「跟随其他模型」：图像模型的服务端点只能出图，跟随会让审阅、清洗这些角色被指到一个用不了的服务上。它既不能跟随别人，也不能被别人跟随；已经存着这种设置的旧配置照常打开，只在下次保存时才要求改掉。
- 「导出含 Key」不再把别人的密钥继续传出去：从别人的配置文件导入到本机的密钥，导出时会被扣下，只有你自己在这台电脑上填过的密钥才会写进文件。连接的其余信息照常导出，对方导入后自己填密钥即可。如果某把密钥本来就是你的，重新填写并保存一次，以后导出就会带上。每次导出含 Key 后会逐条列出哪些密钥跟着走了、哪些被扣下了。

## V9.2.3
- 新增应用内更新：设置「更新与关于」发现新版后可以直接下载、验签、就地替换并重启，不必再去网站手动下载安装包。macOS 就地替换 `.app` 后由用户决定何时重启；Windows 交给 NSIS 安装程序，安装前会先确认是否打断正在跑的任务。走的是 Tauri updater 插件加 minisign 签名的 `latest.json`，签名不对的包一律拒装。
- 更新说明改为在应用内排版显示：二三级标题、列表、加粗与行内代码按 Markdown 渲染，此前是一整段未经处理的原文，`##`、`-`、`**` 全都摆在界面上。发布说明里写给下载页的那半截——签名与 SmartScreen 提示、安装包清单、`.sha256` 校验说明——不再进入应用内：正文以第一条 `---` 分隔线为界，线以下的内容只出现在 GitHub 页面。看这一页的人早就装好了软件，更新器也会自己下载、自己验签。渲染全程用 DOM 拼装，不经 `innerHTML`——发布说明是网络内容。超过四行默认折叠，可展开。
- 启动后台检查一次更新：发现新版时点亮侧栏「设置」红点并在顶栏下方给一条可关掉的提示条。可以在关于页暂停后台提醒，也可以忽略某个具体版本。
- 应用内更新不可用时如实说明原因并退回下载安装包：从 DMG 里直接运行、安装目录不可写、架构不匹配、开发构建等情况各有各的说法，不再给一个点不动的按钮。
- 取更新信息、下载、安装三步失败时分别说明是哪一步断的，附带诊断码，并明确「当前版本没有被改动，可以正常继续使用」。取不到更新信息（这一版没提供应用内更新包，或者网络不通）不再报成「下载失败」——那句话会让人反复重试一件不可能成功的事。
- 装好等重启时点「稍后」不再把这件事整个丢掉：横幅收起来，但页面仍然留着「立即重启」，不会退回去让人把同一个包再下一遍。下载与安装期间重复点击也不会叠出第二个确认框、第二次下载。
- 发布流程随之改造：macOS 的 `.app` 现在自己也走公证与装订（此前只公证 DMG，而应用内更新替换的是 `.app`，不经过 DMG）；更新用的 `.app.tar.gz` 从签名公证后的 bundle 重新打包并签名；Windows 的更新载荷就是 NSIS 安装器本身（Tauri v2 对 NSIS 不再另出压缩包），随包发布它的签名。`latest.json` 在发布任务里生成，任一平台的产物或签名缺失、损坏、版本对不上都直接让发布失败，不会发出一份悄悄少了某个平台的清单；签名还会和应用内置的公钥比对指纹，因为签错密钥在上游只是一句警告，那样的版本会全绿发布、在每台机器上装不上。
- 应用内更新只跟正式发布走：缺 Apple 密钥的临时通道产物没有公证，工作流不再向它下发更新签名密钥，也不生成 `latest.json`——否则那种版本一旦转成正式可见，所有已安装的机器会在没有任何下载动作的情况下被换成未公证版本。反过来，正式发布缺少更新签名密钥时构建直接失败，不再降级成「只发安装包」：那样的 Release 会成为「最新版」，让所有已装机器一直看得见新版本、却永远装不上。
- `scripts/verify_release_metadata.py` 增加应用内更新的接线检查（公钥、端点、两个插件依赖、两条权限），任何一处缺失都会在发布前拦下——它们单独缺失时应用照样能构建、能启动、只是从此再也不更新任何人。

## V9.2.2
- 界面按上一轮反馈逐条修正：任务卡片改用首个文件名做标题；结果清单的「处理」列不再溢出；任务记录可以删除（输出文件不动）；模型名改成右侧下拉；已保存的密钥以掩码写在输入框的占位符里；设置字段失焦即保存；选完文件夹自动开始扫描；日志保留 200 行且不抢滚动位置；新增「无」领域预设；卡片徽标统一为「需复核 xx」；任务详情可以只导出本任务的诊断；记忆库的全选与「已选 xx/xx」归位；选服务商时带出对应的轻档默认模型；新加的连接显示「未测试」；应用图标换成界面里的「文 A」；配置的导入导出收敛成「文档翻译」和「模型服务」两个整包。
- 修复导入自己导出的配置会清空全部 API 密钥：「导出（不含密钥）」再原样导入回来时，每条连接都会被换上新的内部编号，本机存着的密钥随即成为无人认领的孤儿被清理掉——用自己的备份走一圈，密钥全没了。现在端点没变的连接保留原来的编号，密钥原地不动；只有真正消失或换了端点的连接才清理它的密钥。
- 「含密钥导出」再导入时，同一个服务商同一个地址下的备用账号密钥不再顶掉主用账号的。
- 导入配置时若保存设置失败（例如磁盘写满），不再留下已经写进密钥存储的密钥；密钥改为设置保存成功之后才写。
- 导入文档翻译配置不再删掉本机的自定义语言：翻译记忆库的词条按语言存放，删掉一门自定义语言等于让那门语言下的全部译文当场消失。现在两边的自定义语言取并集，本机那条的语言代码保持不变。
- 旧版本导出的配置文件可以正常导入，不再被当作「更高版本」拒绝；文件缺版本号时会说明文件本身不对，不再把用户支去升级应用。
- 导入模型配置的确认按钮改为「确认替换并导入」：连接列表是整份替换的，原来的「确认合并」会让人以为本机自己加的连接还在。
- 修复「测试连接」永远在测主用连接：PDF 翻译模型那条路由不接收连接参数，面板上选中的连接会被丢弃、由主用连接顶替作答；本地运行器则完全无法测试。现在按连接测、按连接记结论，跟随其他角色的结论记在它自己身上。
- 改过密钥之后，旧的「测试通过」结论不再继续挂着。
- 通过服务商作用域取到密钥的第二条连接不再显示成「未填密钥」。
- 修复同一个服务商下配了两个账号时故障转移必然失败：切到第二个账号时拨号用的还是刚被拒绝的那把密钥。
- 保存密钥时会一并写进当前连接：把一条备用连接设为主用后再改密钥，此前新密钥会被这条连接早年留下的旧密钥永久遮住，改多少次都不生效。
- 「本地模型」下切换运行器（Ollama ↔ LM Studio）会一并预填对应地址，不再留着上一个运行器的端口，导致之后每次测试连接都失败而界面上没有任何提示。
- 离开设置页会丢弃没提交的「连接方式」草稿：此前它会一直挂着，下次回到设置页随便改个模型名，失焦自动保存就会连着这份陈年草稿一起提交，把角色悄悄切成本地模式。
- 确认弹窗里的操作失败时会说明原因并保持打开，不再既不关闭也不提示——删除固定词条时最明显，看上去像整个界面卡死。固定词条的「删除」现在直接说明要先解除固定。
- 「需复核 N」不再把应用自己已经处理好的条目算进去（任务详情的「需复核」一格同步改口径）；结果定位清单里未参与翻译的段落不再被标成绿色「已接受」，Excel 的复核类型和处理方式改用中文说明，「单元格原有底色、标记没写进去」不再显示成「已处理」。关闭「标记需复核内容」时会说明本次无法逐格定位。
- 删掉的任务记录不会因为一次在途的轮询又冒出来；删除后左侧列表的空态和右侧详情不再各显示各的。
- 换目录后立刻重新扫描不会再出现「路径栏是新目录、文件清单还是上一个目录」；扫描期间「扫描」和「浏览」置灰。扫描失败会清空旧清单，不拿上一次的结果冒充这一次；「记住上次目录」失败也不再连带清空刚扫出来的清单。
- 任务卡片和详情页的长文件名改为省略号截断，状态徽标和「删除记录」「导出诊断」按钮不再被挤出可视区，完整文件名悬停可见。
- 日志攒满 200 行后往上翻历史不再被新日志一行一行顶走。
- 诊断记录的文件已被清理时，导出会明确说明，不再报服务器错误。

## V9.2.1
- 修复升级后设置与翻译记忆库整体锁死：版本号一律按相等判定，上一版写的 `settings.json`（v24）和 `tm.db`（v2）内容完全有效却被判为不可写，任何保存都返回「设置不是当前可写格式；请在维护页明确重置设置。」，唯一出路是清空整份配置。改为按内容判定——能通过校验的旧配置直接接管并在下次写入时盖上新版本号；记忆库缺列用 `ALTER TABLE ADD COLUMN` 就地补齐，词条一条不动。
- 备份成为重建的前提条件：真正读不动的文件才会被换掉，且必须先备份成功；备份不成就拒绝重建并原样保留原文件，只有维护页的显式重置能越过。此前 `tm.db` 因权限或被占用而打不开时会被无备份删除，实测 50 条词条归零、备份目录为空，而打开设置页就足以触发。
- 被占用的记忆库不再当成损坏文件：探测到写锁只跳过本次版本判定，不备份也不重建。此前一次普通的并发写入（每个任务收尾都会写记忆库）就可能让整个词库被判为不可读并删除。
- 读取不再改写磁盘：加载设置永远只读，修复统一放到启动自检和维护页的健康检查里并全程持锁，读操作不会再和刚完成的保存互相覆盖。
- 新增数据状态提示：有文件被重建时说明备份位置；文件读不出来时说明「原文件没有被覆盖」以及如何处理，不再让用户对着一个正常的页面反复保存失败。
- 「清空翻译记忆库」可以清掉打不开的记忆库文件，不再因为读不出内容而连清空都失败。
- 记忆库存在重复原文时不再彻底打不开：改建普通索引，词条全部保留。
- 新增 DeepSeek 服务商。此前它只在翻译引擎里注册，「获取模型列表」和「测试连接」两个按钮都不认，选了等于不可用。
- 选择服务商时真正预填 Base URL：预设此前只在保存时于后端生效，界面上看起来像没有预设。预填表由后端下发，不在前端留第二份副本。
- 「获取模型列表」「测试连接」不再清空正在填写的表单。草稿按所属连接标记，切换连接的操作（设为主用、新增、删除）会整份丢弃草稿，不会把一条连接的密钥搬到另一条上。

## V9.2.0
- 修复多连接下自动识别源语言整体失效：模型连接池配了两条以上连接、且源语言选「自动识别」时，整份文档会原样输出而任务照报完成。故障转移包装器的两个方法被基类同名实现挡住，转发从未触发，失败又被兜底逻辑改写成「原文即译文」。
- 翻译失败不再冒充成功：批次彻底失败时仍会写出文件（避免已付费的整轮工作作废），但会在日志和文件结果里如实报出「N 条未翻译，这些位置保留的是原文」，并计入需处理项。原先多条一起失败时只统计第一条，计数偏低。
- Excel 单元格超过 32767 字符会被截断并明确报出位置（分表名!坐标），不再写出 Excel 打不开的文件；译文里的 XML 非法控制字符会被清理。补丁改为写在临时文件上、成功后才改名，失败不再在输出目录留下一份用正式文件名命名却未翻译的副本。
- 修复 Excel 组合图形在行高变化后整体变形：只同步顶层形状尺寸，不再连组内每个子形状一起改写。行高调整只作用于本次真正翻译过的行，未翻译区域的手工行高不再被改掉；公式的源码文本不再参与行高估算。补译时的覆盖检测由逐格重解析改为一次读入，含大量公式的表从分钟级降到秒内（6400 个公式格 46.5 秒 → 0.04 秒）。
- 修复 Word 含超链接的段落被写坏：读文本的视图包含超链接、写文本的视图不包含，导致译文后面残留一截未翻译的原文。段落改为按文档真实顺序遍历（跨表格的自动编号不再错乱），「保护封面和目录」不再被编号扁平化绕过，替换模式下的表格单元格改为逐段落写入，不再把嵌套表格和局部格式压平。
- Word 新增内容控件与修订内容的漏译提示：`w:sdt`（内容控件）和未接受的修订插入对提取器不可见，此前静默漏译且无任何提示；现在会报出数量并说明处理方式。
- PDF 页面质检增加内容维度：此前只比对宽高比，模型返回纯白页或糊掉的页面只要比例对就算通过。现在空白页与分辨率过低会标记复核（页面仍进产物），结构性不可用才消耗重试；空白源页不会误报。
- 修复填 `gpt-image-1` 时每页都跑满重试：该模型只能返回固定尺寸，与 1% 的宽高比容差在数学上无法同时满足。容差改为按实际请求的尺寸推算，每页 API 用量从「重试次数+1」降到 1；残余比例差改用补边处理，且不再据此把文件标为需复核。
- PDF 单页损坏不再毁掉整份文件：坏页转为占位页，其余页正常装配。超大幅面页面渲染增加像素上限并自动降低 DPI，不再因一张 A0 图纸耗尽内存。整份文件的页面全部因幅面被跳过时不再报「翻译完成」。占位页补充 Windows 中文字体，不再显示为方框。
- 修复记忆库「清空词库」删不掉固定词条（与「清空未固定词条」是同一份实现）；导入时未标注固定状态的词条不再被一律设为固定；恢复备份时 `overwrite` 模式改为真正覆盖，此前对已存在的词条一律跳过、只在返回里体现为「重复」，用户以为恢复成功。导出备份改为单一快照，不会再出现指向不存在词条的冲突记录。
- 记忆库启用 WAL：此前读写互相阻塞，写入超时后异常还会被吞掉，表现为「保存词条」悄无声息地不生效。翻译收尾写入记忆库失败也不再冲垮整个任务。
- 修复批量固定 / 取消固定词条恒定失败（接口路径被单条操作的路由遮蔽，一直返回 422）。
- 限流不再判死任务：并发降到最低档后改为等待重试，只有持续限速满两分钟才升级为任务失败。批次二分重试增加深度上限和退避，上游抖动时不再把请求量放大近两个数量级。
- 修复 Windows 上「在文件夹中显示」「打开数据目录」「查看 Release」等入口全部无效（一律调用了 macOS 的命令）；启动时不再多弹一个黑色命令行窗口；后台服务启动失败会弹窗说明原因，不再静默闪退，启动超时从 12 秒放宽到 30 秒。
- 修复工作台整轮翻译期间阶段文案卡在「正在准备任务」、逐文件进度不高亮：前端读取的事件字段名与后端发送的不一致。完成横幅的「生成文件数」不再用已选文件数顶替，「需复核」和「自动处理」不再恒为 0。
- 修复设置连续改动互相覆盖：读改写全程纳入已有的跨进程锁并按差异合并，快速切换两个开关不再丢掉先保存的那个。
- 任务事件历史改为节流落盘并移出任务锁：单任务 3000 条事件的额外开销从 19.2 秒降到 0.03 秒。已结束的任务会释放重型引用并限制保留数量，长时间运行不再持续占用内存。
- 新增退出清理：关闭应用会停止运行中的任务并清理 LibreOffice 配置、Word 临时文档和 PDF 页面工作区，中断的任务记为「已中断」而不是一直显示运行中。
- 「页图并发」设置项接入运行时：此前 UI 和命令行写入后无人读取，实际并发始终来自自动推算。
- 关于页不再显示写死的旧版本号，版本一致性检查扩展到界面源码。
- 连点「开始翻译」不再弹出看不懂的报错，按钮在提交期间显示为「正在启动…」。
- 各引擎移除写死的默认模型 ID：模型只能来自用户从实时列表中的选择。

## V9.1.0
- Word 新增「保护封面和目录」：从文档开头一直保留到正文第一个章节标题为止，封面、批准页、目录、前言都不翻译。章节标题按「第一章」「1 概述」「1.1 概述」「（一）」以及 Word 内置标题样式识别，手工敲的目录条目（点线加页码那种）不会被误当成正文起点。识别不到正文起点时不启用保护、全文照常翻译，并在日志和文件结果里说明。全译和补译都生效；原先的「补译时保护方案封面与目录」开关由它取代。
- PDF 新增「跳过 A3 及更大的页面」：工程图纸通常打印成 A3 或更大的幅面，这类页面一般不需要翻译。两个方向都超过 A4 约 15% 的页面不送翻译模型，原始矢量内容整页照搬到输出 PDF，清晰度不变，横放竖放都算。扫描阶段就会显示每个文件有几页大幅面，读不到页面尺寸的文件显示为未知而不是 0；整份都是大幅面的文件在开始翻译前单独标出。被跳过的页在逐页审核里标为「按幅面跳过」，不混进待复核。
- 扫描 Excel 时清点嵌入内容：列出文件里的图片数量和含文字的文本框 / 形状数量，提前说明这两类内容当前不会被翻译。.xls 需要先转换才能看到，此时显示为未知而不是 0。
- 悬浮图片的保护范围扩大：开启「Excel 精调行高」时，整张表的悬浮图片锚点都会在 Excel 重排行高之前固定，位于表格已用区域之外、或使用绝对定位的图片不再被拉伸变形；每个文件会报告本次固定了多少张。
- 语言选择改为可搜索的下拉列表：显示中文名称并附语言代码，不输入也能直接滚动挑选，中文名、英文名和语言代码三种搜法都支持，最近用过的目标语言置顶。记忆库页的语言对筛选一并换成同一个控件。
- 记忆库页新增加载中和加载失败两种状态，加载失败时可以直接重试，不必来回切换页面。
- 键盘焦点圈统一换成与整套配色协调的低饱和描边，浅色和深色下分别取值。
- 修复后端启动稍慢时界面永久失联的问题：此前只要首次连接赶上后端尚未就绪，那次失败会被一直沿用，表现为语言列表空白、目标语言只显示语言代码，且无法自行恢复，只能重启应用。现在失败不再被记住，语言列表空白时也提供「重试」按钮。
- 修复开启「保留原文表」时「悬浮图片已固定尺寸」数量翻倍的问题：原文副本上的是同一批图片，不再重复计数。

## V9.0.0
- 首个 Windows 版本：新增 Windows 10 及以上 x64 安装包（NSIS，含 WebView2 引导），与 macOS Apple Silicon DMG 同步发布；后台更新检查在两个平台分别指向对应安装包。
- 界面全面重设计：Excel、Word、PDF 三个工作区结构统一，任务提交、进度、日志和产物入口集中在同一屏；任务中心、记忆库、设置和帮助页全部重写，支持浅色 / 深色 / 跟随系统主题。
- Excel 翻译改用补丁式写入器：只改动需要翻译的部件，其余部件逐字节保留。嵌入式图片（含 DISPIMG 公式）不再丢失或退化为占位文字，悬浮图片在行高调整后保持原始尺寸不再拉伸变形，含富数据的单元格原样保留。
- PDF 逐页审核升级：审核面板列出全部页面，已通过的页也可以随时一键查看原译对比或重新生成，不再只有待复核的页才能操作。
- 修复命令行翻译的 `--output-dir` 参数对 Excel 与 Word 不生效的问题。
- 新版从独立数据基线开始，不读取或迁移旧版本的设置、密钥与记忆库数据；.xls / .doc 旧格式文档的翻译支持保持不变。

## V8.1.2
- 修复任务日志面板只显示“任务运行日志已脱敏。”的问题：阶段、文件名、词条数、批次进度、耗时、TM 命中和失败原因现在都能看到，凭据仍替换为 [redacted]、绝对路径仍替换为 [path]，引用了原文/译文/提示词/模型响应的整行仍然不下发。
- 修复进度计数被当成路径吃掉的问题：“（1/3）”这类计数不再变成“（1[path]”。
- 发布流程调整：稳定版本标签一律按正式发布出去，不再因为缺少 Apple 签名密钥而标记为 Pre-release；此时安装包仍为 ad-hoc 临时签名且未公证，首次打开需右键“打开”，发布说明中会写明。

## V8.1.1
- 发布精简为 Apple Silicon（M 系列芯片）单架构安装包，不再提供 Intel（x86_64）DMG；Intel Mac 用户请继续使用旧版。
- 修复自动识别源语言功能整体失效的问题：Excel 与 Word 的自动语言任务此前会静默输出原文且不写入记忆库。
- 连接切换更可靠：多批次并发下不再误判并烧掉健康连接；跟随其他用途的角色恢复自动换连接能力；运行中切换沿用任务启动时冻结的密钥；zhipu、dashscope 等免填 Base URL 的服务商不再互相株连。
- 修复两处并发调度卡死：限流降容后排队请求永远等不到槽位、同一任务多线程等待时后来者被永久挂起。
- 翻译失败不再冒充成功：本地 Ollama 重试耗尽、清洗批次失败会如实计入失败并提示；密钥或模型配置错误立即报错，不再空耗数分钟退避重试。
- 修复 PDF 审核解析：合规的审核返回不再被误判为无法解析而白白耗尽每页重试。
- 记忆库更稳健：两个任务同时写入同一新词条不再整批失败；Word 自动识别模式下记忆库命中按每个文件的实际语言范围匹配，避免跨语言误命中。
- 任务体验修复：已不存在的任务事件流正确返回 404，前端不再无限快速重连；任务启动失败不再在历史里留下永久“运行中”的记录；后台检查更新不再覆盖期间保存的设置。
- Windows 侧车探活改用安全方式，修复启动即闪退（为后续 Windows 构建铺路）。
- 其他修复：短密钥掩码不再泄露长度；“导出含 Key”正确携带连接作用域密钥，导入吞吐配置失败会提示；.xls 转换、Word 转换与 Excel 行高精调失败时正确释放 Office 文档，不再残留占用。

## V8.1.0
- 模型配置改为连接池：文档翻译、记忆库清洗、PDF 翻译、PDF 审核各自维护一组连接，每条连接是完整独立的服务商、Base URL、模型和密钥。
- 密钥改用连接自身的作用域存放，同一个 Base URL 下的多个账号可以共存；旧的服务商作用域保留并继续作为回退，降级回旧版本不会丢失已配置的密钥。
- 连接不可用时自动换用下一条：服务端故障会跳过同一 Base URL 的全部连接直接换服务商，密钥被拒或额度耗尽只跳过该条，限流和瞬时超时仍走原有退避重试。
- 新增并行分配开关：关闭时所有任务共用主用连接，打开后同时运行的任务分别占用不同连接，单独运行的任务仍使用主用连接。
- 任务快照记录本次实际使用的连接与候选链，运行中的切换写入任务日志。
- 统一全站输入框视觉：源路径、记忆库搜索等输入框不再透明不可见；模型配置面板的常驻说明收进问号提示，按钮按用途重新命名。
- 修复模型用途、记忆库语言对和每页条数等下拉框点击无效的问题。
- 修复快速开始弹窗按钮被压成竖排、任务中心两块卡片贴合、维护页底部大片留白，以及多处文字回落到 16px 的排版问题。

## V8.0.1
- 补齐 macOS 12 原生 arm64 与 x86_64 自动打包矩阵，发布前执行全量质量、架构和最低系统版本扫描。
- 完成维护、更新、帮助和诊断能力的 macOS-only 发布准备；正式签名、公证和 macOS 12 实机验收按发布门执行。

## V8.0.0
- 桌面壳层改为 Tauri 2，前端以原型同源的 vanilla TypeScript 落地 Excel、Word、PDF、记忆库与模型配置页面。
- Python 文档处理与引擎逻辑保留为受随机端口和启动 token 保护的 FastAPI sidecar；任务进度和日志通过 SSE 实时传递。
- 移除 PySide6/Qt 原生界面与旧打包路径；新版标准发布物为 macOS 原生 DMG，Python sidecar 使用无 Qt 的 PyInstaller onedir 包。
- 新版从独立数据基线开始，不读取、迁移、修复或删除旧版本设置、密钥、翻译记忆库或自定义语言数据。
- 正式 macOS 发布要求 Developer ID 签名、Apple 公证、staple 和 Gatekeeper 验证；手动构建只作为明确标记的未签名测试 artifact。

## V7.4.2
- 将单实例回归测试改为真实独立进程，覆盖 Windows named pipe 的实际线程和事件循环语义，避免用同进程后台线程产生错误结论。
- 保留 V7.4.1 的确认握手与幂等 socket 销毁修复，并在创建发布 tag 前增加无发布的双平台 CI 预检。

## V7.4.1
- 修复 Windows 单实例激活握手：第二次启动在主实例确认收到激活消息后再正常断开，避免 named pipe 在客户端过早退出时丢失消息。
- 增加跨线程的双实例回归测试，覆盖主实例收到激活后立即关闭及后续 Qt 延迟销毁事件，防止退出崩溃再次出现。

## V7.4
- 修复中断与关闭生命周期：后台任务静默退出或消息异常时进入可恢复终态，窗口关闭采用有界等待，中断后可返回并开始新任务。
- 增加原子任务资源租约，避免快速连续点击时让共用 API 的 Excel、Word、PDF 与记忆库清洗任务竞态启动；不同 API 仍可并行。
- 连接测试和模型列表获取改为后台执行，关闭窗口时取消回调，减少网络异常造成的界面假死或退出卡住。
- 增加原生应用单实例保护；重复启动会激活现有窗口，不再并行打开多个进程争用设置、密钥和记忆库。
- 强化翻译正确性与失败语义：修复 DISPIMG 普通文本、同名文件状态、双语覆盖识别、TM 反向词条、CLI 全失败退出码和记忆库批次失败误报完成。
- PDF 的 API 槽位等待支持取消并保证租约恰好释放；审核接口异常会保留候选译图并标记人工复核，不再重复生成整页增加费用。
- 设置、密钥及模型配置导出改用并发锁和原子写入；POSIX 密钥文件权限为 `0600`，损坏密钥文件不会被静默覆盖。
- DashScope API Key 改为请求级传入，避免不同密钥的并行任务互相污染；文本和图像请求设置明确超时。
- 精简未使用的 pandas、python-dotenv、rich 直接依赖，并为 Python 3.11 发布环境增加完整约束、冻结包 smoke test 与双平台 CI 验证。
- 修正 macOS 安装包的最低系统声明为实际二进制要求的 macOS 15.0；签名、公证及 Windows Authenticode 支持通过发布环境变量启用并验证。

## V7.3
- 修复 PDF 翻译目标语言默认值：PDF 页默认使用中文，并独立记住 PDF 翻译页上次选择的目标语言。
- PDF 整页译图质检移除固定分辨率门槛，不再将比例正确但像素较低的有效返回图误判为失败。
- PDF 图片生成默认并发改为更保守的 2，降低高并发下接口波动导致的页面失败概率。
- 增强 OpenAI 兼容图片编辑接口兼容性：遇到部分参数不支持的 400 响应时自动降级请求参数并保留详细错误信息。

## V7.2
- 修复 Word 表格翻译遗漏：嵌套表格内容会按内部单元格独立提取和回填，不再被外层单元格合并后漏译。
- Word 输出后新增覆盖率复查，若仍发现疑似未译源文，会写入质量报告并给出表格/段落位置。
- 优化 Word 译文段落格式继承，减少首行缩进、分页、标题样式和空白尾段造成的版式异常。
- 语言选择支持更多中文别名搜索和配置归一化，例如“法语/法文”“汉语/中文”，并避免刷新选项后目标语言回退。
- 启动时会自动补迁移旧版缺失数据文件，不覆盖当前已有设置和词库。

## V7.1
- 升级 GitHub Actions 分发构建工作流，使用 Node 24 兼容的 checkout、setup-python、upload-artifact 和 release 上传 action 版本。
- 固定 Windows 与 macOS 构建 runner，避免 `windows-latest`、`macos-latest` 迁移导致自动打包环境漂移。
- 保持 V7.0 小屏适配功能不变，作为自动打包基础设施升级版本发布。

## V7.0
- 小屏启动改为按屏幕可用区域自适应，避免 13/14 英寸屏幕首次打开时窗口超出屏幕。
- 新增小屏窄导航栏，完整模型、领域和 Prompt 设置可通过“设置”面板打开，让主工作区保留更多横向空间。
- 记忆库管理页保留顶部三张卡片横排，并为小屏提供安全紧凑宽度，避免语言下拉和操作按钮被硬压。
- 补充原生主窗口小屏布局回归测试，覆盖紧凑模式宽度、记忆库卡片行对齐和语言控件可读性。

## V6.9
- 修复 Word 自动编号兜底物化：跳过空白编号段，不再让“第一节”被空段占位挤成“第二节”，并保留中文编号样式如“第一节”。
- Word 复核标记改为写入真实 `w:highlight` 文本高亮，默认映射为 WPS 可清除的高亮标记，不再使用难以清除的运行底纹。
- 修复返回新任务、重置、空扫描和再次扫描后的按钮状态复发问题，补齐 stale table、stale label、扫描线程和更新检查线程的生命周期防护。
- Word 译文会继承原段落开头用空格或全角空格手工做出的缩进，避免只继承段落格式时丢失这类视觉缩进。
- Excel 与 Word 新增“只补未译内容”入口，可在双语文件中仅处理缺少译文的位置，已有译文保持不变，并补充覆盖率识别与并发调度测试。

## V6.8
- 强化 PDF 版式翻译提示词，按目标语言补充更明确的排版、表格和文字保留约束，降低漏译、串格和阿拉伯语表格镜像风险。
- 增强 Word 自动编号预处理：支持通过本地 Word 或 LibreOffice 将自动编号转为稳定文本，减少双语 Word 输出中的编号丢失或错位。
- 优化原生界面控件、提示文本、模型配置说明和 GitHub 仓库入口，提升侧边栏与翻译页的操作提示一致性。
- 补充 PDF 提示词、Word 编号、原生组合框、侧边栏和翻译页面相关回归测试，保留当前改造前的稳定发布快照。

## V6.7
- 放宽 Excel、Word、PDF 翻译任务互斥：不同类型任务可在使用不同 API 时并行运行。
- 保留同类型任务最多运行一个的限制，并在 API footprint 重叠时继续锁定启动入口。
- PDF 翻译会同时计算图片生成模型与审核模型的 API 占用，避免审核模型与其他任务共用同一 API 时并发冲突。
- 翻译任务启动时冻结模型设置与 API Key 快照，运行中切换侧边栏配置不会影响已启动任务。
- Excel 翻译新增“标记需复核内容”开关，可像 Word 翻译一样选择是否在输出文件中写入风险底色或红字。

## V6.6
- 新增 Excel 与 Word 复核标记颜色配置，可分别设置语义校验接受、原文保留复核和疑似原文异常三类标记颜色。
- 颜色下拉项直接展示对应底色，并在默认预设名称后标注“默认”；自定义颜色支持系统选色器和 `#RRGGBB` 色值输入。
- Excel 与 Word 输出写入逻辑改为读取统一颜色配置，保留已有底色或高亮时按页面策略处理冲突。
- 强化混合语言与异常文本识别后的复核标记写入，补充 Word/Excel 输出和原生页面回归测试。

## V6.5
- 架构升级前的稳定快照版本：跳过已舍弃的 V6.1 至 V6.4 发布编号，重新以 V6.5 保留当前可发布状态。
- 保持当前单一翻译任务流程，运行中的 Excel、Word、PDF 翻译任务仍需完成或终止后再开始下一任务。
- 保留架构升级前的重建基线决策文档，便于后续翻译逻辑升级时回溯当前稳定版本。

## V6.4
- 移除翻译任务排队机制：运行中的 Excel、Word、PDF 翻译任务不再允许继续安排下一任务，需等待完成或终止后再返回开始新任务。
- 清理翻译列表、队列快照、队列顺序调整和队列历史相关界面入口，降低结果页返回后再次翻译的状态同步复杂度。

## V6.3
- 重构模型配置导入导出格式，按翻译模型、深度清洗模型、PDF 翻译模型和 PDF 翻译审核模型完整保存当前界面配置。
- 导出文件同步保存服务商、Base URL、模型名称、API Key、配置来源和吞吐参数，导入后可复现当前模型配置。
- 支持同一服务商在不同 Base URL 下分别保存和导入 API Key，避免多套 OpenAI 兼容配置互相覆盖。
- 调整模型吞吐参数为按实际模型配置保存，切换模型后可恢复对应批次大小和并发数。

## V6.2
- 新增云端模型配置导入与导出入口，放置在侧边栏“模型配置”顶部，便于迁移云端服务商、Base URL、模型名称和相关 API Key。
- 导入导出仅处理云端模型配置；本地模型服务、模型名称和本地并发设置不会写入导出文件，也不会被导入覆盖。
- 优化模型配置状态显示：无状态内容时不再保留空白提示行。
- 统一精简侧边栏、翻译页面、词库页面和连接检查提示文案，使界面表述更正式、清晰、简洁。

## V6.1
- 新加入 PDF 翻译功能：可将 PDF 页面或图片按版式生成译文图像/PDF，并保留页面素材、翻译报告和复核信息，便于后续人工检查与归档。
- 新增翻译任务排队机制：可以在当前任务运行时继续添加多个 Excel、Word、PDF 翻译任务，支持查看翻译列表、调整排队顺序、取消待执行任务和清理历史记录。
- 优化原生界面细节：源路径、自定义输出目录和结果路径在空间不足时采用中间省略显示，任务清单统计卡片更紧凑，减少长路径和队列界面对布局的干扰。
- 强化队列 UI 回归测试：自动化点击测试覆盖 Excel、Word、PDF 三类任务的扫描、排队、返回、取消、清空历史和停止/终止等关键按钮状态。

## V5.0
- 重大升级：Translator 从“网页打包出来的桌面窗口”升级为真正的原生桌面应用。界面更像正式软件，打开更直接，操作更稳定，安装包也更精简。
- 界面体验全面优化：Excel 翻译、Word 翻译和术语库页面都改为原生界面，按钮、表格、文件选择、进度显示和结果查看更顺手，不再像在浏览器页面里操作。
- Windows 和 macOS 都有正式安装包：Windows 下载 `Translator_Windows_5.0_Setup.exe`，macOS 下载 `Translator_macOS_5.0.dmg`，普通用户不需要自己配置源码环境。
- Word 翻译进入正式主线：可以直接处理 Word 文档，支持批量翻译、旧 `.doc` 文档转换、翻译失败后的自动重试，以及需要人工复核内容的高亮提示。
- Excel 翻译流程更稳：旧 `.xls` 文件兼容性更好，Windows 上也能更好地调用本机 Excel；如果当前电脑没有可用 Excel，也会提示使用兼容模式继续处理。
- 结果页更清楚：任务完成后可以看到每个文件是否生成、哪些内容需要复核、问题大概出在哪里，不用只靠滚动日志猜结果。
- 诊断和排查更方便：遇到翻译失败、API 异常或个别内容无法处理时，可以导出诊断包，方便后续定位问题和修复。
- 旧网页包装版本仍保留在 V4.6；从 V5.0 开始，后续功能都会围绕真正的原生桌面应用继续优化。

## V4.6
- 图标换成了可爱帅气的凉前辈。
- 项目显示名调整为 Translator，并将应用名、安装包名和打包配置收拢到统一元数据。
- 本地数据目录切换为平台原生位置，并在首次启动时提供旧目录 `~/.xl_translator` 的确认迁移流程。
- 新增 API 并发/限流反馈的自适应降并发处理，Excel、Word 批量翻译和 Word 语义仲裁会在上游限流时降低并发并重试当前批次。
- macOS / Windows 打包配置统一改用通用文件名，并按当前版本生成新版安装包名称。

## V4.5
- 新增 Excel / Word 翻译异常诊断归档：任务报错、文件失败或 API 单条失败时会自动保存轻量诊断记录，并可下载当前诊断包。
- 新增历史诊断统一导出入口，便于隔一段时间集中导出问题归档用于程序修复；默认诊断包不包含原始文件和 API Key。
- Excel API 单条失败会记录失败原文摘要与哈希，并生成单元格定位清单；Word 会沿用段落/表格位置和质量提示生成定位清单。
- 新增翻译前 API 配置预检，缺少 API Key、Base URL、模型或 Hermes 路由时会提前提示，避免任务进入后台后才失败。
- 新增启动时 API 健康提示和侧边栏模型列表获取，方便及时发现配置失效并选择可用模型。
- 优化 OpenAI / Claude / OpenAI 兼容配置隔离，官方 OpenAI 不再继承自定义 Base URL，并兼容旧版蓝翼/自定义 Key 存储。
- 优化 Excel 批次调度：按字符预算拆分批次，失败后自动缩小批次重试，并记录实际批次数、重试次数和失败项。

## V4.4
- Word 翻译新增并行恢复池：首轮严格校验失败后，会并发执行单段重试与语义仲裁，任一路径可靠通过即写入译文。
- Word 恢复路径会保留首轮被规则拦截的候选译文，并在安全护栏内交由大模型判断源文与候选译文是否完整等义。
- 新增加权 API 并发调度：正文批量翻译、单段重试、语义仲裁共享用户设置的总并发预算，长批次按预估 token 权重占用更多槽位。
- 语义仲裁通过的 Word 译文会高亮原文并写入质量报告，但不会写入翻译记忆库；Excel 严格校验规则保持不变。

## V4.3
- macOS 12 Monterey 及更早系统自动改用默认浏览器打开应用页面，避免旧版系统 WebView 出现白屏。
- macOS 13 及以上系统继续优先使用内置桌面窗口。

## V4.2
- 新增应用内版本检测，可从侧边栏检查 GitHub 最新版本并跳转下载新版安装包。
- 新增 API / Ollama 连接测试按钮，配置完成后可先验证服务、模型与密钥是否可用。
- 修复官方 OpenAI / Claude 配置可能误继承自定义 OpenAI Base URL 的问题。
- Word 翻译“每批最多段落”默认值调整为 8，并将可选范围扩展为 1-16。

## V4.1
- 修复 macOS Monterey 12.x 启动时 NumPy C 扩展无法加载的问题。
- macOS 发布流程强制使用兼容旧系统的 NumPy wheel，并在打包后检查 `NEWLAPACK/ILP64` 符号，避免再次生成不兼容安装包。
- macOS 发布构建固定使用 Python 3.11，防止本地高版本 Python runtime 误进入安装包。

## V4.0
- 新增应用内桌面窗口体验：Streamlit 本地服务启动后优先打开 Translator 桌面窗口，不再直接依赖系统浏览器。
- 新增 `pywebview` 运行依赖，并在 WebView 不可用时自动回退到系统浏览器。
- Streamlit 服务默认绑定 `127.0.0.1`，启动参数关闭使用统计，减少本机服务暴露和启动噪音。
- 应用窗口关闭后会自动清理对应 Streamlit 进程，改善安装版与源码版的退出行为。
- PyInstaller 打包配置补齐 WebView/桌面窗口相关隐式依赖，Windows 与 macOS 安装包都带上内置窗口能力。
- 发布流程移除无版本号 release assets，统一保留带版本号的安装包与校验文件。

## V3.3
- 统一翻译质量校验核心，保留 Excel 严格校验路径，Word 仅在单段重试恢复阶段启用更宽松的恢复策略。
- 优化 Word 正文金额与数量校验，支持 `2000万` 与 `20,000,000` 等语义等价数字。
- Word 单段重试阶段可自动接受疑似 OCR 噪声数字导致的可用译文，并在质量提示中展示问题片段、在输出 Word 中高亮整段原文。
- Word 恢复策略接受的译文不写入 TM，避免将需复核结果固化到翻译记忆库。

## V3.2
- 优化 Word 翻译失败恢复策略：重试后仍无有效译文的段落会按配置继续执行多轮单段严格重试。
- Word 高级批次策略新增失败重试次数设置，默认 3 轮，可在 1-8 轮之间调整。
- 新增可选的 Word 复核高亮功能，开启后会在生成的双语 Word 中标记最终仍需人工复核的段落或表格单元格。
- 复核高亮默认关闭，并会避开已有底纹或文本高亮，避免覆盖原文档已有颜色标记。

## V3.1
- 修复 macOS/Windows 冻结安装包启动时缺少 Streamlit 依赖元数据的问题。
- 修复 PyInstaller 环境中 Streamlit 被误判为开发模式，导致本地端口配置失效的问题。
- 发布新的双端安装包，用于替换 `V3.0` 中可能无法正常打开的分发产物。

## V3.0
- 版本号简化为两位格式，从 `V3.0` 开始表示正式大版本发布。
- 新增 Word 翻译工作区与 Word 独立批次策略。
- 修复 Word 自动编号在双语译文中丢失或继续参与自动编号的问题。
- 新增更多内置目标语言支持，并支持任意语言翻译成中文。
- 标准化双端安装包发布流程：Windows 输出安装器，macOS 输出 dmg。
- 标准发布产物不再包含 Windows portable zip 或 macOS zip。

## V2.2.0
- 新增更多内置目标语言支持。
- 新增任意语言翻译成中文的能力。
- 新增 Word 翻译工作区与 Word 独立批次策略。
- 新增 Windows 启动入口和 GitHub Actions Windows 分发包构建流程。
- 修复 Word 自动编号在双语译文中丢失或继续参与自动编号的问题。

## V2.1.0
- 修复“公式生成的显示文本”在翻译后无法正确回填的问题。
- 新增可选项“公式文本按显示值回填”，默认开启。
- 开启后会按公式显示值匹配翻译并回填。
- 命中后对应单元格会改写为静态文本，不再保留原公式。

## V2.0.1
- 调整启动方式：第一次启动会显示初始化过程，初始化完成后，后续启动默认保持静默启动。
- 静默启动失败时，会自动切回可见启动方式。
- 更新了启动脚本、首次使用说明和分发相关说明。

## V2.0.0
- 新增统一的版本信息管理。
- 新增应用图标和侧边栏品牌显示。
- 分发包文件名开始带版本号，方便区分和保留历史版本。
- 打包时会一并带上应用图标和版本信息。
- 更新了分发流程和相关说明文档。

## V1.3.0
- 调整了翻译页的页面布局。
- 新增系统原生文件夹选择和 Excel 文件选择功能。
- 调整了术语库（TM）管理页的搜索、分页、删除确认和提示词设置相关功能。
- 增加了整应用和页面视觉检查相关的自测支持。

## V1.2.0
- 新增项目自测规范和自测工具。
- 目标语言选择从英文、法文两项，调整为统一的目标语言选择方式。
- 内置目标语言从英文、法文扩展为多种语言。
- 新增自定义目标语言功能。
- 新增最近使用目标语言记录功能。
- 翻译页和术语库（TM）页改为共用同一套目标语言处理逻辑。

## V1.0.0
- 提供基础应用结构。
- 提供 Excel 翻译功能。
- 提供术语库（TM）管理功能。
- 提供基础设置功能。
- 提供启动脚本和分发脚本。
- 初始支持英文和法文。
