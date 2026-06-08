# VidMind Light UI Spec

> 说明：本文件保留为浅色模式视觉提案。当前开发与实现请优先参考 `docs/vidmind-ui-spec.md`。若与当前代码不一致，以 `apps/desktop/src/app.css` 和主规范为准，尤其是深色模式与平台标签实现。

## 1. 白天模式重设计说明

- 设计方向：明亮、轻专业、带一点消费级产品的精致感，避免传统后台的厚重感。
- 品牌气质：从 VidMind logo 提取 `#FB7299 / #F85D8E / #FF9ABA` 作为暖粉珊瑚品牌主轴，用低饱和浅底承接。
- 产品属性表达：
  - 主操作区强调“输入链接 -> 本地处理 -> AI 总结”
  - 数据概览退居第二层，不与主 CTA 抢焦点
  - 视频库作为主要内容浏览区，承接资产管理与结果消费
- 低频信息策略：服务状态、版本、运行设备收纳到左侧底部状态面板，降低对主流程的打扰。

## 2. 页面布局结构

### 左侧导航

- 窄边栏，品牌卡作为视觉锚点
- 导航项采用浅底圆角选中态，激活态使用品牌弱高亮
- 底部状态面板承接服务状态、最近任务、版本、GPU/CPU

### 主内容区

- 顶部：页面标题 + 一句产品说明 + 服务在线 badge
- 第一层：主操作卡
  - 长输入框
  - 明显 CTA
  - 辅助提示与预览卡
- 第二层：视频库概览卡
  - 4 个统计指标
  - 底部一条最近更新 insight
- 第三层：视频库内容区
  - 标题
  - 搜索框
  - 状态筛选 pill
  - 视频卡片网格

### 扩展性

- 详情页沿用同一套卡片、描边、圆角和信息层级
- 设置页使用相同 token，可持续扩展到任务详情页 / 视频详情页 / 设置页

## 3. 配色方案

### 基础色

- 页面底色：`#F7F8FC`
- 内容浅底：`#FCFCFE`
- 卡片底色：`#FFFFFF`
- 弱强调底：`rgba(251, 114, 153, 0.08)`

### 品牌色

- Brand 400: `#FF9ABA`
- Brand 500: `#FB7299`
- Brand 600: `#F85D8E`
- Brand 700: `#D94674`

### 文字色

- 主文字：`#1F2937`
- 次级文字：`#556274`
- 辅助文字：`#7D8898`
- 弱提示：`#98A2B3`

### 边框与结构色

- 细描边：`rgba(148, 163, 184, 0.18)`
- 默认边框：`rgba(129, 140, 160, 0.28)`
- 强边框：`rgba(100, 116, 139, 0.42)`

### 状态色

- 在线 / 成功：`#22A866`
- 处理中：`#D58918`
- 错误：`#E14B5A`
- 信息：`#567EFF`

## 4. 字体层级建议

- 页面主标题：`36-52px`，`700`，`-0.04em`
- 模块标题：`24-32px`，`700`
- 卡片标题：`15-18px`，`600-700`
- 正文：`14px`，`400-500`
- 辅助说明：`13-14px`，`400`
- 数据数字：`28-40px`，`700-800`
- 标签 / kicker / 状态 pill：`11-12px`，`700`，大写或弱字距

推荐字体：

- Sans：`Plus Jakarta Sans / Manrope / PingFang SC / Noto Sans SC`
- Mono：`JetBrains Mono`

## 5. 组件视觉规范

### 卡片

- 圆角：`18 / 22 / 28px`
- 描边：1px 低对比灰蓝边框
- 阴影：柔和浅阴影，不使用重发光
- 高亮方式：浅品牌色铺底，不用大面积霓虹

### 按钮

- 主按钮：品牌渐变 `#FB7299 -> #F85D8E`
- 高度：`48-56px`
- 圆角：`16px`
- Hover：轻微上浮 + 阴影增强

### 输入框

- 高度：`56px`
- 左侧图标 + 长 placeholder
- Focus：品牌色外描边 `rgba(251, 114, 153, 0.12)`

### 导航栏

- 宽度建议：`240-248px`
- 激活态：浅品牌底 + 细描边
- 图标容器独立，形成轻工具感

### 视频卡片

- 封面优先，占比大
- 标题最多两行
- 上层：平台 + 状态
- 下层：更新日期 / 结果状态
- Hover：轻微上浮、描边增强、阴影变深

## 6. React + Tailwind 结构示意

当前项目已直接落地为 React + 自定义 token CSS；如果前端团队要按 Tailwind 组件化继续实现，可按下列结构拆：

```tsx
export function VidMindHome() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <div className="grid min-h-screen grid-cols-[248px_minmax(0,1fr)]">
        <aside className="sticky top-0 flex min-h-screen flex-col gap-4 border-r border-slate-200/70 bg-white/70 p-5 backdrop-blur">
          <div className="rounded-3xl border border-rose-200/70 bg-gradient-to-b from-white to-rose-50 p-4 shadow-sm">
            <BrandCard />
          </div>
          <nav className="grid gap-2">
            <NavItem active>视频库</NavItem>
            <NavItem>设置</NavItem>
          </nav>
          <StatusPanel className="mt-auto" />
        </aside>

        <main className="mx-auto w-full max-w-[1440px] px-7 py-6">
          <header className="mb-6 flex items-end justify-between gap-4">
            <PageHeader />
            <ServiceBadge />
          </header>

          <section className="grid gap-5 xl:grid-cols-[minmax(0,1.42fr)_minmax(340px,0.95fr)]">
            <PrimaryActionCard />
            <OverviewCard />
          </section>

          <section className="mt-6 rounded-[28px] border border-slate-200/70 bg-white/90 p-6 shadow-sm">
            <LibraryHeader />
            <FilterRow />
            <VideoGrid />
          </section>
        </main>
      </div>
    </div>
  );
}
```

### Tailwind Token 建议

```ts
colors: {
  brand: {
    50: "#FFF4F7",
    100: "#FFE8EE",
    200: "#FFD2DE",
    300: "#FFB3C9",
    400: "#FF9ABA",
    500: "#FB7299",
    600: "#F85D8E",
    700: "#D94674",
  },
  ink: {
    900: "#1F2937",
    700: "#556274",
    500: "#7D8898",
    400: "#98A2B3",
  },
}
borderRadius: {
  card: "28px",
  panel: "22px",
  control: "16px",
}
boxShadow: {
  soft: "0 10px 24px rgba(15,23,42,0.06)",
  card: "0 22px 40px rgba(15,23,42,0.08)",
}
```
