# 文档页设计系统（Docs Design System）

> 本文件是项目**说明文档页**（非 Web 控制台）的设计系统声明。三份文档页共用
> 同一套 token 与结构族是**有意的系统化设计**，不是模板复用：文档矩阵需要统一
> 的视觉身份，读者在页与页之间切换时不应感到"换了个网站"。
> 每页 `<style>` 首行均带 Hallmark stamp 声明对本系统的遵从。

## 适用页面

| 页面 | 角色 | Stamp |
|---|---|---|
| `年报风险识别系统专用readme.html` | 部署指南（运行时 `/readme` 可访问） | Long Document · atmospheric-docs |
| `年报风险识别系统 · 操作指南.html` | 全生命周期操作手册 | Long Document · atmospheric-docs |
| `答辩QA速查.html` | 技术答辩问答 + 修复清单存档 | Long Document · atmospheric-docs |

不适用：`src/web/index.html`（功能控制台，独立演进）、`baka专用readme.html`（个人趣味页，豁免）。

## 宏观结构族

**Long Document**：单栏长文流，首屏 hero（标题 + 版本徽标）→ 章节卡片序列 → 页脚
免责声明。答辩QA速查 额外允许左侧粘性目录栏（文档超长时的导航例外）。

## Tokens

### 色彩（暗色氛围底，蓝色单锚点）

- 背景：深海军蓝渐变/径向氛围底（`#0f172a` ~ `#0a0e17` 族），**不用纯黑**
- 锚点色：蓝 `#3b82f6` / `#60a5fa` 一族；紫色仅作 QA 页辅助角色色，不做主锚
- 语义色：danger 红 / warning 橙 / success 绿 / info 蓝，仅用于提示框与优先级标签
- 文字：主 `#e2e8f0`，弱化 `#94a3b8`，标题近白实色

### 字体（双字体配对）

- 展示（h1/h2）：`--font-display: "Source Han Serif SC", "Noto Serif SC", "STZhongsong", "SimSun", serif`
- 正文：`'Segoe UI', "PingFang SC", "Microsoft YaHei", sans-serif`
- 代码：`'Fira Code', 'Consolas', monospace`

## 结构与组件规则（v3.1 起强制）

1. **标题实色**：h1/h2 一律实色墨字（近白），禁止 `background-clip: text` 渐变字
2. **边框 hairline**：提示框/清单卡用 1px 全边框 + 语义色（低透明度），禁止单侧
   4px 粗色条（side-stripe）
3. **暗底海拔用亮度**：层级靠表面亮度区分，禁止彩色光晕 box-shadow / text-shadow
4. **入场动效只此一次**：仅首屏 hero 一个 0.6s ease-out 入场；正文内容静置，
   禁止整页滚动渐入（IntersectionObserver 逐卡 fade-up）与无限循环漂浮动画
5. **过渡指明属性**：禁止 `transition: all`，逐项声明 background-color/color/border-color
6. **毛玻璃克制**：backdrop-filter 仅用于承载层（卡片/侧栏），不做纯装饰叠加
7. **图标**：现阶段接受 emoji 图标（跨平台文档页、零依赖）；如替换须整站统一
   一套 SVG 库，禁止混用

## 演进约定

- 新增文档页必须遵从本系统并携带 stamp；偏离本系统 = design-system drift
- 修改 token 先改本文件，再同步各页（单一事实来源）
- 本系统只管文档页；Web 控制台如需设计系统另立文件
