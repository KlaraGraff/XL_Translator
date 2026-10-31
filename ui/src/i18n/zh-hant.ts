// 繁體中文界面：词条照旧以简体中文为键，显示前用 OpenCC（简体 → 台湾正体，含台湾用词）
// 换成繁体，不另维护一份繁体词条。转换表约 1 MB，只在选了繁體中文时才加载。
//
// OpenCC 的台湾用词表偏程序员说法，少数词放在本软件里不合适（「参数」→「引數」、
// 「审核」→「稽核」、「高级」→「高階」……），转换后再按下面的表改回通行说法。
// 改这张表时跑 tests/i18n_engine.test.mjs，并抽查设置页、任务页的截图。

export type Converter = (text: string) => string;

// [转换后的说法, 想要的说法]。按顺序替换，长的写在前面。
const WORDING_FIXES: ReadonlyArray<readonly [RegExp, string]> = [
  [/稽核/g, "審核"],
  [/擴充套件/g, "擴充功能"],
  [/型別/g, "類型"],
  [/引數/g, "參數"],
  [/許可權/g, "權限"],
  [/區域性/g, "局部"],
  [/全域性/g, "全域"],
  [/原始檔夾/g, "來源資料夾"],
  [/原始檔(?![案夾])/g, "原始檔案"],
  [/高階/g, "進階"],
  [/畫素/g, "像素"],
  [/對映/g, "對應"],
  [/釋出/g, "發布"],
  [/重灌/g, "重新安裝"],
  [/映象/g, "鏡像"],
  [/反饋/g, "回饋"],
  [/質量/g, "品質"],
  [/獲取/g, "取得"],
  [/賬號/g, "帳號"],
  [/賬戶/g, "帳戶"],
  [/自定義/g, "自訂"],
  [/文本框/g, "文字方塊"],
  [/文本/g, "文字"],
  [/當前/g, "目前"],
  [/響應/g, "回應"],
  [/通過「/g, "透過「"],
  [/迴圈/g, "循環"],
];

let converter: Converter | null = null;
let loading: Promise<Converter> | null = null;

function withWordingFixes(base: Converter): Converter {
  return (text: string) => {
    let out = base(text);
    for (const [pattern, replacement] of WORDING_FIXES) out = out.replace(pattern, replacement);
    return out;
  };
}

/** 已经加载好的转换函数；还没加载时返回 null。 */
export function traditionalConverter(): Converter | null {
  return converter;
}

/** 加载简体 → 繁体（台湾）转换表。重复调用只加载一次。 */
export function loadTraditionalConverter(): Promise<Converter> {
  if (converter) return Promise.resolve(converter);
  if (!loading) {
    loading = import("opencc-js/cn2t")
      .then((OpenCC) => {
        converter = withWordingFixes(OpenCC.Converter({ from: "cn", to: "twp" }));
        return converter;
      })
      .catch((error: unknown) => {
        loading = null;
        throw error;
      });
  }
  return loading;
}

/** 测试用：直接给一个转换函数（node 里不走动态加载）。 */
export function createTraditionalConverter(base: Converter): Converter {
  return withWordingFixes(base);
}
