// 原地更新：把新建好的一棵 DOM 套到页面上已有的那棵上，能留的旧节点留着，只改属性和文字。
//
// 任务页、工作区每来一个进度事件都会整块重画。新节点一插进去，进度条上的流光、状态灯的
// 闪烁、进度框边上的流动光带都从头再播一遍；鼠标停着的那颗按钮也是新的，悬停色先断一下
// 再淡回来，事件一密就一闪一闪。留下旧节点就不会。
//
// 难点在监听器：留下的是旧节点和它当初绑的监听器，新节点上的监听器连同它闭包里抓着的
// 新节点一起被丢掉。所以只留两类节点：
//   1. 整棵子树一个监听器都没有的（纯展示：进度条、流水线、芯片、文字）；
//   2. 调用方明确说「可以留」的：markMorphBoundary 标的容器（自己不绑监听器，子节点各自
//      按这套规则比），和 setReusableClick 绑点击的按钮（点击处理只认任务状态，不抓节点，
//      留旧按钮时把新的处理函数换上去）。
// 其余带监听器的子树（输入框、下拉、折叠块……）照旧整块换成新的，行为和以前一样。

/** 绑过监听器的元素。只增不减：漏记会把带监听器的节点当成纯展示留下，多记只是少留几个。 */
const listened = new WeakSet<EventTarget>();
const nativeAddEventListener = EventTarget.prototype.addEventListener;

if (typeof Element !== "undefined") {
  EventTarget.prototype.addEventListener = function addEventListener(
    this: EventTarget,
    type: string,
    listener: EventListenerOrEventListenerObject | null,
    options?: boolean | AddEventListenerOptions,
  ): void {
    if (this instanceof Element) listened.add(this);
    nativeAddEventListener.call(this, type, listener, options);
  };
}

const boundaries = new WeakSet<Element>();
const reusableClicks = new WeakMap<Element, { handler: (event: MouseEvent) => void }>();

/** 这个容器本身不绑监听器，原地更新时留旧的、逐个比子节点（子节点各自按规则留或换）。 */
export function markMorphBoundary<T extends Element>(node: T): T {
  boundaries.add(node);
  return node;
}

/** 绑一个原地更新时可以跟着旧按钮留下的点击：处理函数不能抓这次新建的任何节点。 */
export function setReusableClick(node: Element, handler: (event: MouseEvent) => void): void {
  const slot = { handler };
  reusableClicks.set(node, slot);
  nativeAddEventListener.call(node, "click", (event) => slot.handler(event as MouseEvent));
}

function pureSubtree(node: Element, memo: Map<Element, boolean>): boolean {
  const known = memo.get(node);
  if (known !== undefined) return known;
  let pure = !listened.has(node);
  if (pure) {
    for (const child of Array.from(node.children)) {
      if (!pureSubtree(child, memo)) {
        pure = false;
        break;
      }
    }
  }
  memo.set(node, pure);
  return pure;
}

function nodeKey(node: Node): string {
  if (!(node instanceof Element)) return `#${node.nodeType}`;
  const key = node.getAttribute("data-key") ?? node.getAttribute("data-task-id") ?? node.getAttribute("data-seq") ?? "";
  return `${node.namespaceURI}|${node.tagName}|${node.classList[0] ?? ""}|${key}`;
}

/** 入场动画的已播时长（--res-elapsed 这类）每画一次都不一样，但节点留下时动画本来就接着播，
 *  改它反而让正在播的动画跳一截。比 style 时不看这些变量。 */
const ELAPSED_VAR = /--[\w-]*elapsed\s*:[^;]*;?/g;

/** 把 fresh 的属性（含 class、style、title、data-*、disabled）和表单值搬到 old 上。 */
export function syncAttributes(old: Element, fresh: Element): void {
  for (const attr of Array.from(old.attributes)) {
    if (!fresh.hasAttributeNS(attr.namespaceURI, attr.localName)) old.removeAttributeNS(attr.namespaceURI, attr.localName);
  }
  for (const attr of Array.from(fresh.attributes)) {
    const current = old.getAttributeNS(attr.namespaceURI, attr.localName);
    if (current === attr.value) continue;
    if (attr.name === "style" && current !== null && current.replace(ELAPSED_VAR, "").trim() === attr.value.replace(ELAPSED_VAR, "").trim()) continue;
    old.setAttributeNS(attr.namespaceURI, attr.name, attr.value);
  }
  // 属性之外的表单状态（没绑监听器的只读输入框、勾选框）。
  if (old instanceof HTMLInputElement && fresh instanceof HTMLInputElement) {
    if (old.value !== fresh.value) old.value = fresh.value;
    if (old.checked !== fresh.checked) old.checked = fresh.checked;
  } else if (old instanceof HTMLTextAreaElement && fresh instanceof HTMLTextAreaElement && old.value !== fresh.value) {
    old.value = fresh.value;
  }
}

function canKeep(old: Element, fresh: Element, memo: Map<Element, boolean>): boolean {
  if (old.namespaceURI !== fresh.namespaceURI || old.tagName !== fresh.tagName) return false;
  const oldClick = reusableClicks.get(old);
  const freshClick = reusableClicks.get(fresh);
  if (oldClick && freshClick) return !listened.has(old) && !listened.has(fresh);
  if (oldClick || freshClick || listened.has(old) || listened.has(fresh)) return false;
  if (boundaries.has(old) && boundaries.has(fresh)) return true;
  return pureSubtree(old, memo) && pureSubtree(fresh, memo);
}

/**
 * 把 old 更新成 fresh 的样子，返回最后留在文档里的节点（留得住就是 old，否则 fresh 替换了它）。
 * fresh 用完即弃，调用后不要再用。
 */
export function morphNode(old: Node, fresh: Node, memo = new Map<Element, boolean>()): Node {
  if (old instanceof Element && fresh instanceof Element) {
    if (!canKeep(old, fresh, memo)) {
      old.parentNode?.replaceChild(fresh, old);
      return fresh;
    }
    syncAttributes(old, fresh);
    const oldClick = reusableClicks.get(old);
    const freshClick = reusableClicks.get(fresh);
    if (oldClick && freshClick) oldClick.handler = freshClick.handler;
    morphChildren(old, fresh, memo);
    return old;
  }
  if ((old.nodeType === Node.TEXT_NODE || old.nodeType === Node.COMMENT_NODE) && old.nodeType === fresh.nodeType) {
    if (old.nodeValue !== fresh.nodeValue) old.nodeValue = fresh.nodeValue;
    return old;
  }
  old.parentNode?.replaceChild(fresh, old);
  return fresh;
}

/** 把 parent 的子节点更新成 fresh 的子节点：同类（标签 + 第一个类名 + data-key）的按先后配对。 */
export function morphChildren(parent: Element, fresh: Element, memo = new Map<Element, boolean>()): void {
  const remaining = Array.from(parent.childNodes);
  const pairs: Array<[Node | null, Node]> = [];
  for (const next of Array.from(fresh.childNodes)) {
    const key = nodeKey(next);
    const index = remaining.findIndex((node) => nodeKey(node) === key);
    pairs.push([index >= 0 ? remaining.splice(index, 1)[0] : null, next]);
  }
  for (const node of remaining) parent.removeChild(node);
  pairs.forEach(([old, next], index) => {
    const at = parent.childNodes[index] ?? null;
    if (!old) {
      if (at !== next) parent.insertBefore(next, at);
      return;
    }
    // 先把留下的旧节点放到位，再原地比较；配不上时 morphNode 就地换成新节点。
    if (at !== old) parent.insertBefore(old, at);
    morphNode(old, next, memo);
  });
}
