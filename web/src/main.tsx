import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type PurchaseScope = "company" | "unreviewed" | "excluded" | "all";
type Platform = "taobao" | "jd";
type View =
  "overview" | "orders" | "merchant" | "invoices" | "reports" | "settings";
type Profile = {
  company_name: string;
  tax_id: string;
  address: string;
  phone: string;
  bank_name: string;
  bank_account: string;
  email: string;
  allow_ordinary: boolean;
  configured?: boolean;
};
type Decision = {
  queue: string;
  action: string;
  label: string;
  reason: string;
};
type Order = {
  id: string;
  platform: Platform;
  order_no: string;
  product: string;
  shop: string;
  amount_fen: number;
  paid_at: string;
  trade_status: string;
  invoice_status: string;
  invoice_type: string;
  special_status: string;
  ordinary_supported: boolean;
  evidence: string;
  company_purchase: boolean;
  scope_excluded?: boolean;
  scope_reason?: string;
  order_url: string;
  invoice_url: string;
  invoice_buyer: string;
  invoice_tax_id: string;
  source: string;
  decision: Decision;
  updated_at: string;
};
type Invoice = {
  id: string;
  order_id: string;
  platform: Platform;
  order_no: string;
  product: string;
  invoice_type: string;
  amount_fen: number;
  filename: string;
  sha256: string;
  created_at: string;
  download_url: string;
};
type Job = {
  id: string;
  kind: string;
  status: string;
  message: string;
  created_at: string;
};
type Event = {
  id: string;
  at: string;
  kind: string;
  message: string;
  order_id?: string;
};
type Connection = { platform: Platform; status: string; message: string };
type Bootstrap = {
  token: string;
  workspace: string;
  live_submit_enabled: boolean;
  orders: Order[];
  summary: {
    total: number;
    actionable: number;
    special: number;
    merchant: number;
    downloaded: number;
    amount_fen: number;
    company_total?: number;
    excluded?: number;
  };
  profile: Profile;
  connections: Connection[];
  invoices: Invoice[];
  jobs: Job[];
  events: Event[];
  version: string;
  data_dir: string;
};
type Preview = {
  id: string;
  order_id: string;
  platform: Platform;
  order_no: string;
  product: string;
  shop: string;
  invoice_type: string;
  company_name: string;
  tax_id: string;
  address: string;
  phone: string;
  bank_name: string;
  bank_account: string;
  email: string;
  amount_fen: number;
  expires_at: string;
  workspace: string;
  message: string;
};
const ErrorContext = React.createContext("");
const emptyProfile: Profile = {
  company_name: "",
  tax_id: "",
  address: "",
  phone: "",
  bank_name: "",
  bank_account: "",
  email: "",
  allow_ordinary: false,
};
const navs: [View, string, string][] = [
  ["overview", "总览", "grid"],
  ["orders", "订单队列", "list"],
  ["merchant", "商家沟通", "chat"],
  ["invoices", "发票仓库", "folder"],
  ["reports", "月度报销", "chart"],
  ["settings", "设置", "settings"],
];
const money = (fen: number) =>
  (fen / 100).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
const platformName = (p: string) => (p === "taobao" ? "淘宝" : "京东");
const invoiceName = (p: string) =>
  p === "special"
    ? "增值税专用发票"
    : p === "ordinary"
      ? "增值税普通发票"
      : "票种待确认";
const statusNames: Record<string, string> = {
  unrequested: "尚未申请",
  pending: "等待开票",
  issued: "已开票",
  downloaded: "已归档",
  rejected: "申请被拒",
  unknown: "待核实",
  completed: "交易完成",
  paid: "已付款",
  unpaid: "待付款",
  refunded: "已全额退款",
  partial_refund: "部分退款",
  closed: "交易关闭",
};
const sourceNames: Record<string, string> = {
  manual: "手动导入",
  browser: "平台页面",
};
const date = (s?: string) =>
  s
    ? new Date(s).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const localDay = () => new Date().toLocaleDateString("sv-SE");
function isCompanyPurchase(order: Order) {
  return order.company_purchase && !order.scope_excluded;
}
function selectPurchaseScope(orders: Order[], scope: PurchaseScope) {
  if (scope === "company") return orders.filter(isCompanyPurchase);
  if (scope === "excluded") return orders.filter((o) => o.scope_excluded);
  if (scope === "unreviewed")
    return orders.filter((o) => !o.company_purchase && !o.scope_excluded);
  return orders;
}
function companyPurchaseAmount(orders: Order[]) {
  return orders
    .filter((o) => isCompanyPurchase(o) && ["paid", "completed"].includes(o.trade_status))
    .reduce((amount, o) => amount + o.amount_fen, 0);
}
function monthlyCompanyOrders(orders: Order[], month: string) {
  return orders.filter((o) => isCompanyPurchase(o) && o.paid_at?.slice(0, 7) === month);
}
function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    list: (
      <>
        <rect x="4" y="3" width="16" height="18" rx="2" />
        <path d="M8 8h8M8 12h8M8 16h5" />
      </>
    ),
    chat: (
      <path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8A8.5 8.5 0 0 1 12.5 20a8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 3.9a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z" />
    ),
    folder: (
      <path d="M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" />
    ),
    chart: (
      <>
        <path d="M4 3v17h17M8 16v-5M13 16V7M18 16V4" />
      </>
    ),
    settings: (
      <>
        <path d="m12 3 2 3 3-.2.6 3 2.4 2-2 2.5.1 3-3 .6-2 2.4-2.5-2-3 .1-.6-3L3 12l2-2.5-.1-3 3-.6L10 3Z" />
        <circle cx="12" cy="12" r="3" />
      </>
    ),
    download: (
      <>
        <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />
      </>
    ),
    plus: <path d="M12 4v16M4 12h16" />,
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2" />
      </>
    ),
    search: (
      <>
        <circle cx="10.5" cy="10.5" r="6.5" />
        <path d="m16 16 5 5" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z" />
        <path d="m8 12 3 3 5-6" />
      </>
    ),
    external: (
      <>
        <path d="M14 3h7v7m0-7L10 14" />
        <path d="M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5" />
      </>
    ),
    copy: (
      <>
        <rect x="8" y="8" width="13" height="13" rx="2" />
        <path d="M16 8V3H3v13h5" />
      </>
    ),
    receipt: (
      <>
        <path d="M5 3h14v18l-3-2-4 2-4-2-3 2V3Z" />
        <path d="M9 7h6M9 11h6M9 15h3" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.receipt}
    </svg>
  );
}
function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = React.useId();
  const error = React.useContext(ErrorContext);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "wide" : ""}`}
      aria-labelledby={titleId}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="modal-heading">
        <h2 id={titleId}>{title}</h2>
        <button className="icon-button" aria-label="关闭" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          <p>{error}</p>
        </div>
      )}
      {children}
    </dialog>
  );
}
function App() {
  const [view, setView] = useState<View>("overview");
  const [data, setData] = useState<Bootstrap | null>(null);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [queue, setQueue] = useState("all");
  const [platform, setPlatform] = useState("all");
  const [purchaseScope, setPurchaseScope] = useState<PurchaseScope>("company");
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [draft, setDraft] = useState<{ text: string; kind: string } | null>(
    null,
  );
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importName, setImportName] = useState("");
  const [profile, setProfile] = useState<Profile>(emptyProfile);
  const [month, setMonth] = useState(localDay().slice(0, 7));
  const [startDate, setStartDate] = useState(localDay().slice(0, 7) + "-01");
  const [endDate, setEndDate] = useState(localDay());
  const [maxPages, setMaxPages] = useState(3);
  const [scanPlatform, setScanPlatform] = useState<Platform>("taobao");
  const [scanOpen, setScanOpen] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const dataRef = useRef<Bootstrap | null>(null);
  dataRef.current = data;
  async function api(path: string, body?: unknown, method?: string) {
    const r = await fetch("/api" + path, {
      method: method || (body === undefined ? "GET" : "POST"),
      headers: {
        "Content-Type": "application/json",
        "X-Workspace": "live",
        "X-Local-Token": dataRef.current?.token || "",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!r.ok) {
      let d;
      try {
        d = await r.json();
      } catch {
        throw new Error(`请求失败（${r.status}），请检查本地服务是否运行。`);
      }
      throw new Error(
        typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail),
      );
    }
    return r.json();
  }
  async function refresh() {
    const next = (await api("/bootstrap")) as Bootstrap;
    if (next.workspace !== "live") {
      throw new Error("当前工作区无法确认，请刷新页面后重试。");
    }
    setData(next);
  }
  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.has("workspace")) {
      url.searchParams.delete("workspace");
      window.history.replaceState(null, "", url);
    }
    refresh().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (data) setProfile({ ...emptyProfile, ...data.profile });
  }, [data?.workspace, data?.profile.company_name, data?.profile.tax_id]);
  useEffect(() => {
    const timer = setInterval(() => {
      if (dataRef.current?.jobs.some((j) => j.status === "running"))
        refresh().catch((e) => setError(e.message));
    }, 1800);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 4500);
    return () => clearTimeout(timer);
  }, [toast]);
  async function run(fn: () => Promise<unknown>, success?: string) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
      if (success) setToast(success);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const orders = data?.orders || [];
  const liveSubmitPaused = data?.live_submit_enabled !== true;
  const companyOrders = selectPurchaseScope(orders, "company");
  const unreviewedOrders = selectPurchaseScope(orders, "unreviewed");
  const excludedOrders = selectPurchaseScope(orders, "excluded");
  const scopedOrders = selectPurchaseScope(orders, purchaseScope);
  const current = orders.find((o) => o.id === selected);
  const filtered = scopedOrders.filter(
    (o) =>
      (platform === "all" || o.platform === platform) &&
      (queue === "all" || o.decision.queue === queue) &&
      `${o.product} ${o.shop} ${o.order_no}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const merchantOrders = companyOrders.filter(
    (o) =>
      o.decision.action === "ask_special" ||
      o.invoice_status === "pending" ||
      o.invoice_status === "rejected",
  );
  const monthOrders = monthlyCompanyOrders(orders, month);
  const companyInvoices = (data?.invoices || []).filter((i) =>
    companyOrders.some((o) => o.id === i.order_id),
  );
  const monthInvoices = (data?.invoices || []).filter((i) =>
    monthOrders.some((o) => o.id === i.order_id),
  );
  const running = data?.jobs.filter((j) => j.status === "running").length || 0;
  const needsAttention =
    data?.jobs.filter(
      (j) => j.status === "failed" || j.status === "needs_attention",
    ).length || 0;
  const unresolvedOrders = orders.filter((o) => o.decision.label === "核对提交结果");
  async function showPreview(o: Order) {
    await run(async () =>
      setPreview(await api(`/orders/${encodeURIComponent(o.id)}/preview`, {})),
    );
  }
  async function showDraft(o: Order, kind = "ask") {
    await run(async () =>
      setDraft(
        await api(`/orders/${encodeURIComponent(o.id)}/draft?kind=${kind}`),
      ),
    );
  }
  async function saveOrder(o: Order, patch: unknown) {
    await run(
      () => api(`/orders/${encodeURIComponent(o.id)}`, patch, "PATCH"),
      "订单信息已保存",
    );
  }
  async function downloadFile(path: string, filename: string) {
    await run(async () => {
      const r = await fetch(path, {
        headers: { "X-Workspace": "live", "X-Local-Token": data?.token || "" },
      });
      if (!r.ok) throw new Error("文件下载失败，请刷新发票仓库后重试。");
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  function template() {
    const content =
      "platform,order_no,product,shop,amount_fen,paid_at,trade_status,invoice_status\n";
    const url = URL.createObjectURL(
      new Blob(["\uFEFF" + content], { type: "text/csv;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "订单导入模板.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function parseCsv(text: string) {
    const rows: string[][] = [];
    let row: string[] = [],
      value = "",
      quoted = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (c === '"') {
        if (quoted && text[i + 1] === '"') {
          value += '"';
          i++;
        } else quoted = !quoted;
      } else if (c === "," && !quoted) {
        row.push(value);
        value = "";
      } else if ((c === "\n" || c === "\r") && !quoted) {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(value);
        if (row.some(Boolean)) rows.push(row);
        row = [];
        value = "";
      } else value += c;
    }
    if (quoted) throw new Error("CSV 有未闭合的引号，请检查文件。");
    row.push(value);
    if (row.some(Boolean)) rows.push(row);
    if (rows.length < 2) throw new Error("文件需要表头和至少一条订单。");
    const headers = rows.shift()!.map((h) => h.trim().replace(/^\uFEFF/, ""));
    return rows.map((r, i) => {
      if (r.length !== headers.length)
        throw new Error(`CSV 第 ${i + 2} 行列数不匹配。`);
      const obj: Record<string, unknown> = {};
      headers.forEach((h, n) => {
        if (h === "amount_fen") obj[h] = Number(r[n]);
        else if (["company_purchase", "ordinary_supported"].includes(h))
          obj[h] = ["true", "1", "是"].includes(r[n].trim().toLowerCase());
        else if (r[n] !== "") obj[h] = r[n].trim();
      });
      return obj;
    });
  }
  async function importOrders() {
    await run(async () => {
      const raw = importText.trim().replace(/^\uFEFF/, "");
      let values;
      if (raw.startsWith("[") || raw.startsWith("{")) {
        const p = JSON.parse(raw);
        values = Array.isArray(p) ? p : p.orders;
      } else values = parseCsv(raw);
      if (!Array.isArray(values) || !values.length)
        throw new Error("请导入非空订单数组。");
      const r = await api("/orders/import", { orders: values });
      setImportOpen(false);
      setImportText("");
      setToast(`已导入 ${r.imported} 条，跳过 ${r.skipped} 条重复；新记录尚未纳入公司采购，可在“待核对”查看`);
    });
  }
  const actionButton = (o: Order, compact = false) => {
    if (!isCompanyPurchase(o))
      return (
        <button
          className={compact ? "text-button muted" : "button"}
          onClick={(e) => {
            e.stopPropagation();
            setSelected(o.id);
          }}
        >
          {o.scope_excluded ? "查看排除原因" : "查看导入记录"}
        </button>
      );
    if (["apply_special", "apply_ordinary"].includes(o.decision.action))
      return (
        <button
          className={compact ? "text-button" : "button primary"}
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            showPreview(o);
          }}
        >
          {liveSubmitPaused ? "预览申请" : "核对申请"}{compact && <Icon name="arrow" size={14} />}
        </button>
      );
    if (o.decision.action === "download")
      return (
        <button
          className={compact ? "text-button" : "button primary"}
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            run(
              () => api(`/orders/${encodeURIComponent(o.id)}/download`, {}),
              "下载任务已创建，请查看任务进度",
            );
          }}
        >
          <Icon name="download" size={15} />
          下载归档
        </button>
      );
    if (o.decision.action === "ask_special")
      return (
        <button
          className={compact ? "text-button" : "button primary"}
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            showDraft(o);
          }}
        >
          生成询票草稿
        </button>
      );
    return (
      <button
        className={compact ? "text-button muted" : "button"}
        onClick={(e) => {
          e.stopPropagation();
          setSelected(o.id);
        }}
      >
        查看详情{compact && <Icon name="arrow" size={14} />}
      </button>
    );
  };
  const renderOrders = (items: Order[], limit?: number) =>
    items.length ? (
      <div className="table-scroll">
        <table className="orders-table">
          <thead>
            <tr>
              <th>商品与订单</th>
              <th>平台 / 商家</th>
              <th className="numeric">实付金额</th>
              <th>采购范围</th>
              <th>处理状态</th>
              <th>下一步</th>
            </tr>
          </thead>
          <tbody>
            {items.slice(0, limit).map((o) => (
              <tr
                key={o.id}
                onClick={() => setSelected(o.id)}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter") setSelected(o.id);
                }}
              >
                <td>
                  <div className="product-name">{o.product}</div>
                  <div className="subtle order-number">
                    {o.order_no} <span>· {o.paid_at}</span>
                  </div>
                </td>
                <td>
                  <span className={`platform-pill ${o.platform}`}>
                    {platformName(o.platform)}
                  </span>
                  <div className="subtle shop">{o.shop}</div>
                </td>
                <td className="numeric money">¥ {money(o.amount_fen)}</td>
                <td>
                  <span className={`purchase-badge ${o.scope_excluded ? "excluded" : isCompanyPurchase(o) ? "company" : "unreviewed"}`}>
                    {o.scope_excluded ? "已排除本次范围" : isCompanyPurchase(o) ? "已确认公司采购" : "未纳入公司采购"}
                  </span>
                  {o.scope_excluded && o.scope_reason && <div className="scope-reason">{o.scope_reason}</div>}
                </td>
                <td>
                  <span
                    className={`status status-${o.decision.queue.toLowerCase()}`}
                  >
                    <i />
                    {isCompanyPurchase(o) ? o.decision.label : o.scope_excluded ? "不进入取票队列" : "仅保留导入记录"}
                  </span>
                </td>
                <td>{actionButton(o, true)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    ) : (
      <div className="empty small">
        <Icon name="list" size={36} />
        <h3>{orders.length ? "当前范围没有订单" : "这里还没有导入订单"}</h3>
        <p>
          {orders.length
            ? "公司采购视图只展示已明确纳入的订单，其他记录可在“全部导入”查看。"
            : "连接购物账号扫描订单，或导入已有订单清单。"}
        </p>
        {!orders.length && (
          <button className="button" onClick={() => setImportOpen(true)}>
            导入订单
          </button>
        )}
      </div>
    );

  return (
    <ErrorContext.Provider value={error}>
      <div className="app-shell">
        <aside className="sidebar">
          <a
            className="brand"
            href="#"
            onClick={(e) => {
              e.preventDefault();
              setView("overview");
            }}
          >
            <span className="brand-mark">
              <Icon name="receipt" size={26} />
            </span>
            <span>
              票夹<small>电商取票工作台</small>
            </span>
          </a>
          <div className="workspace-label">
            <span className="connection-dot" />
            本地工作区
          </div>
          <nav aria-label="主导航">
            {navs.map(([id, label, icon]) => (
              <button
                key={id}
                className={view === id ? "active" : ""}
                aria-label={label}
                aria-current={view === id ? "page" : undefined}
                title={label}
                onClick={() => {
                  setView(id);
                  setQuery("");
                  setQueue("all");
                  setPlatform("all");
                  setPurchaseScope("company");
                }}
              >
                <Icon name={icon} />
                <span>{label}</span>
                {id === "merchant" && merchantOrders.length > 0 && (
                  <b>{merchantOrders.length}</b>
                )}
              </button>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <div className="privacy-note">
              <Icon name="shield" size={19} />
              <div>
                数据留在本机<small>订单、企业资料与发票独立保存</small>
              </div>
            </div>
            <span className="version">票夹 {data?.version || "0.1.0"}</span>
          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar">
            <div className="breadcrumb">
              我的工作台 <span>/</span> {navs.find((n) => n[0] === view)?.[1]}
            </div>
            <div className="topbar-actions">
              <button className="task-button" onClick={() => setLogOpen(true)}>
                <Icon name="clock" size={17} />
                {running
                  ? `${running} 个任务运行中`
                  : needsAttention
                    ? `${needsAttention} 项异常记录`
                    : "任务与记录"}
                {running > 0 && <span className="pulsing-dot" />}
              </button>
              <span className="local-badge">仅本机</span>
            </div>
          </header>
          {data && liveSubmitPaused && (
            <div className="preview-only-banner" role="note">
              <Icon name="shield" size={17} />
              <span>真实提交已暂停，可先核对订单和申请资料。</span>
            </div>
          )}
          <main>
            <div className="page-title">
              <div>
                <h1>
                  {navs.find((n) => n[0] === view)?.[1] === "总览"
                    ? "把发票，一张张收齐。"
                    : navs.find((n) => n[0] === view)?.[1]}
                </h1>
                <p>
                  {view === "overview"
                    ? "淘宝与京东订单统一处理，专票优先，每一步有据可查。"
                    : view === "orders"
                      ? "默认只处理已确认公司采购。其他导入记录单独查看，不自动申请。"
                      : view === "merchant"
                        ? "按订单生成询票、申请与催票草稿，核对后自行发送。"
                        : view === "invoices"
                          ? "保留原始电子文件，按订单归档并检查重复文件。"
                          : view === "reports"
                            ? "仅汇总已确认公司采购和关联发票，导出报销准备清单。"
                            : "管理企业抬头、购物账号连接和本地扫描。"}
                </p>
              </div>
              <div className="page-actions">
                {view !== "settings" && (
                  <button
                    className="button"
                    disabled={busy}
                    onClick={() => setImportOpen(true)}
                  >
                    <Icon name="plus" size={17} />
                    导入订单
                  </button>
                )}
                <button
                  className="icon-button refresh-button"
                  aria-label="刷新数据"
                  disabled={busy}
                  onClick={() => run(() => refresh(), "数据已刷新")}
                >
                  <Icon name="refresh" />
                </button>
                {["overview", "orders"].includes(view) && (
                  <button
                    className="button primary"
                    onClick={() => setScanOpen(true)}
                    disabled={busy}
                  >
                    <Icon name="refresh" size={17} />
                    扫描订单
                  </button>
                )}
              </div>
            </div>
            {error && (
              <div className="error-banner" role="alert">
                <div>
                  <b>操作未完成</b>
                  <p>{error}</p>
                </div>
                <button aria-label="关闭错误" onClick={() => setError("")}>
                  <Icon name="close" size={18} />
                </button>
              </div>
            )}
            {unresolvedOrders.length > 0 && (
              <div className="scope-attention" role="alert">
                <div><b>{unresolvedOrders.length} 笔申请结果尚待核对</b><p>采购范围筛选不会解除提交结果不明的记录。请在官方页面核对已有申请，避免重复操作。</p></div>
                <button className="button" onClick={() => setLogOpen(true)}>查看全部任务记录</button>
              </div>
            )}
            {!data ? (
              <div className="loading-state">
                <span className="spinner" />
                {error ? "等待连接本地服务" : "正在读取本地工作区…"}
              </div>
            ) : (
              <>
                {view === "overview" && (
                  <>
                    <div className="scope-summary" role="note">
                      <span>全部导入 <b>{orders.length}</b> 笔</span>
                      <span>已确认公司采购 <b>{companyOrders.length}</b> 笔</span>
                      <span>未纳入公司采购 <b>{unreviewedOrders.length}</b> 笔</span>
                      <span>已排除 <b>{excludedOrders.length}</b> 笔</span>
                    </div>
                    <section className="metrics" aria-label="订单概览">
                      <div className="metric metric-primary">
                        <div className="metric-label">
                          {liveSubmitPaused ? "可核对订单" : "可直接处理"} <Icon name="list" size={18} />
                        </div>
                        <strong>
                          {companyOrders.filter((o) => ["P0", "P1", "P3"].includes(o.decision.queue)).length}
                          <span>笔</span>
                        </strong>
                        <div className="metric-foot">
                          已确认公司采购 {companyOrders.length} 笔
                        </div>
                      </div>
                      <button
                        className="metric"
                        onClick={() => {
                          setView("orders");
                          setPurchaseScope("company");
                          setQueue("P1");
                        }}
                      >
                        <div className="metric-label">
                          {liveSubmitPaused ? "专票候选" : "可申请专票"} <span className="mini-badge">优先</span>
                        </div>
                        <strong>
                          {companyOrders.filter((o) => o.decision.queue === "P1").length}
                          <span>笔</span>
                        </strong>
                        <div className="metric-foot">
                          已确认专票支持 <Icon name="arrow" size={15} />
                        </div>
                      </button>
                      <button
                        className="metric"
                        onClick={() => setView("merchant")}
                      >
                        <div className="metric-label">
                          待联系商家 <Icon name="chat" size={18} />
                        </div>
                        <strong>
                          {merchantOrders.length}
                          <span>笔</span>
                        </strong>
                        <div className="metric-foot">
                          先询问，再决定票种 <Icon name="arrow" size={15} />
                        </div>
                      </button>
                      <button
                        className="metric"
                        onClick={() => setView("invoices")}
                      >
                        <div className="metric-label">
                          已归档发票 <Icon name="folder" size={18} />
                        </div>
                        <strong>
                          {companyInvoices.length}
                          <span>张</span>
                        </strong>
                        <div className="metric-foot">
                          公司采购金额 ¥{money(companyPurchaseAmount(orders))}
                        </div>
                      </button>
                    </section>
                    {!data.profile.configured && (
                      <div className="setup-callout">
                        <div className="callout-icon">
                          <Icon name="receipt" size={23} />
                        </div>
                        <div>
                          <h3>先设置好公司开票资料</h3>
                          <p>
                            填写公司抬头与税号，之后申请时自动带入，并在提交前逐项核对。
                          </p>
                        </div>
                        <button
                          className="button"
                          onClick={() => setView("settings")}
                        >
                          设置企业资料 <Icon name="arrow" size={16} />
                        </button>
                      </div>
                    )}
                    <div className="overview-grid">
                      <section className="panel queue-panel">
                        <div className="panel-title">
                          <div>
                            <h2>公司采购取票队列</h2>
                            <p>仅展示已确认公司采购，按开票优先级排序</p>
                          </div>
                          <button
                            className="text-button"
                            onClick={() => { setView("orders"); setPurchaseScope("company"); setQueue("all"); setPlatform("all"); setQuery(""); }}
                          >
                            查看公司采购 <Icon name="arrow" size={15} />
                          </button>
                        </div>
                        {renderOrders(
                          companyOrders.filter(
                            (o) => !["DONE", "WAIT"].includes(o.decision.queue),
                          ),
                          6,
                        )}
                      </section>
                      <aside className="panel workflow-panel">
                        <h2>取票处理顺序</h2>
                        <ol className="workflow">
                          <li>
                            <span className="workflow-dot">
                              <Icon name="download" size={16} />
                            </span>
                            <div>
                              <b>收好已开的发票</b>
                              <p>下载原始文件，关联订单</p>
                            </div>
                          </li>
                          <li>
                            <span className="workflow-dot">
                              <Icon name="check" size={16} />
                            </span>
                            <div>
                              <b>优先申请专票</b>
                              <p>确认支持后再提交申请</p>
                            </div>
                          </li>
                          <li>
                            <span className="workflow-dot">
                              <Icon name="chat" size={16} />
                            </span>
                            <div>
                              <b>不明确，就先询问</b>
                              <p>生成商家消息，保留证据</p>
                            </div>
                          </li>
                          <li>
                            <span className="workflow-dot">
                              <Icon name="receipt" size={16} />
                            </span>
                            <div>
                              <b>普票作为备选</b>
                              <p>明确无法开专票时处理</p>
                            </div>
                          </li>
                        </ol>
                        <div className="workflow-note">
                          退款、金额异常与票种未知的订单，会单独留待核实。
                        </div>
                      </aside>
                    </div>
                    <section className="panel recent-panel">
                      <div className="panel-title">
                        <h2>最近处理记录</h2>
                        <button
                          className="text-button"
                          onClick={() => setLogOpen(true)}
                        >
                          查看记录 <Icon name="arrow" size={15} />
                        </button>
                      </div>
                      {data.events.length ? (
                        <div className="event-list">
                          {data.events.slice(0, 4).map((e, i) => (
                            <div key={e.id || i} className="event-row">
                              <span className="event-dot" />
                              <span>{e.message}</span>
                              <time>{date(e.at)}</time>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className="quiet-text">
                          还没有处理记录。扫描或导入第一批订单后，从这里跟进每一步。
                        </p>
                      )}
                    </section>
                  </>
                )}
                {view === "orders" && (
                  <section className="panel">
                    <div className="scope-filter" aria-label="采购范围筛选">
                      {([
                        ["company", "公司采购", companyOrders.length],
                        ["unreviewed", "待核对", unreviewedOrders.length],
                        ["excluded", "已排除", excludedOrders.length],
                        ["all", "全部导入", orders.length],
                      ] as [PurchaseScope, string, number][]).map(([id, label, count]) => (
                        <button key={id} className={purchaseScope === id ? "selected" : ""} aria-pressed={purchaseScope === id} onClick={() => { setPurchaseScope(id); setQueue("all"); }}>
                          {label}<span>{count}</span>
                        </button>
                      ))}
                    </div>
                    <p className="scope-filter-note">
                      {purchaseScope === "company" ? "仅展示已确认公司采购。导入记录不会自动成为公司采购。" : purchaseScope === "excluded" ? "这些订单已排除本次取票范围，保留来源记录，不申请公司发票。" : purchaseScope === "unreviewed" ? "这些记录尚未纳入公司采购，不进入取票流程。私人消费可直接排除，无需确认成公司采购。" : "这里包含全部导入记录，包括未纳入和已排除的订单；导入数量不代表公司采购数量。"}
                    </p>
                    <div className="filter-bar">
                      <div className="queue-tabs">
                        {[
                          ["all", "全部状态"],
                          ["P0", "待下载"],
                          ["P1", "专票优先"],
                          ["P2", "待询问"],
                          ["P3", "普票备选"],
                          ["MANUAL", "待核实"],
                          ["WAIT", "等待中"],
                          ["DONE", "处理结束"],
                        ].map(([k, text]) => (
                          <button
                            className={queue === k ? "selected" : ""}
                            key={k}
                            onClick={() => setQueue(k)}
                          >
                            {text}
                            <span>
                              {k === "all"
                                ? scopedOrders.length
                                : scopedOrders.filter((o) => o.decision.queue === k)
                                    .length}
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="table-toolbar">
                      <label className="search-field">
                        <Icon name="search" size={17} />
                        <input
                          aria-label="搜索订单"
                          placeholder="搜索商品、商家或订单号"
                          value={query}
                          onChange={(e) => setQuery(e.target.value)}
                        />
                      </label>
                      <select
                        aria-label="筛选平台"
                        value={platform}
                        onChange={(e) => setPlatform(e.target.value)}
                      >
                        <option value="all">全部平台</option>
                        <option value="taobao">淘宝</option>
                        <option value="jd">京东</option>
                      </select>
                      <span className="result-count">
                        {filtered.length} 笔订单
                      </span>
                    </div>
                    {renderOrders(filtered)}
                  </section>
                )}
                {view === "merchant" && (
                  <section className="panel">
                    <div className="panel-title">
                      <h2>
                        商家待沟通{" "}
                        <span className="count-badge">
                          {merchantOrders.length}
                        </span>
                      </h2>
                      <span className="subtle">草稿不会自动发送</span>
                    </div>
                    {merchantOrders.length ? (
                      <div className="merchant-list">
                        {merchantOrders.map((o) => (
                          <article key={o.id} className="merchant-card">
                            <div className={`merchant-avatar ${o.platform}`}>
                              {platformName(o.platform).slice(0, 1)}
                            </div>
                            <div className="merchant-info">
                              <h3>{o.shop}</h3>
                              <p>
                                {o.product} <span>¥{money(o.amount_fen)}</span>
                              </p>
                              <small>订单 {o.order_no}</small>
                              <div className="reason-text">
                                {o.decision.reason}
                              </div>
                            </div>
                            <div className="merchant-actions">
                              <span
                                className={`status status-${o.decision.queue.toLowerCase()}`}
                              >
                                {o.decision.label}
                              </span>
                              <button
                                className="button"
                                disabled={busy}
                                onClick={() =>
                                  showDraft(
                                    o,
                                    o.invoice_status === "pending"
                                      ? "remind"
                                      : "ask",
                                  )
                                }
                              >
                                <Icon name="chat" size={16} />
                                {o.invoice_status === "pending"
                                  ? "生成催票草稿"
                                  : "生成询票草稿"}
                              </button>
                              <button
                                className="text-button"
                                onClick={() => setSelected(o.id)}
                              >
                                记录商家答复
                              </button>
                            </div>
                          </article>
                        ))}
                      </div>
                    ) : (
                      <div className="empty">
                        <Icon name="chat" size={40} />
                        <h3>暂无需要联系的商家</h3>
                        <p>票种不明、等待开票的订单会出现在这里。</p>
                      </div>
                    )}
                  </section>
                )}
                {view === "invoices" && (
                  <section className="panel">
                    <div className="panel-title">
                      <div>
                        <h2>
                          原始发票文件{" "}
                          <span className="count-badge">
                          {companyInvoices.length}
                          </span>
                        </h2>
                        <p>
                          仅展示已确认公司采购的文件；票种与金额引用订单记录，归档不代表已完成税务验真。
                        </p>
                      </div>
                    </div>
                    {companyInvoices.length ? (
                      <div className="invoice-grid">
                        {companyInvoices.map((i) => (
                          <article className="invoice-card" key={i.id}>
                            <div className="invoice-top">
                              <span className="file-icon">
                                <Icon name="receipt" size={26} />
                              </span>
                              <span className="status status-wait">
                                尚未验真
                              </span>
                            </div>
                            <h3>{i.product}</h3>
                            <p>{invoiceName(i.invoice_type)}</p>
                            <strong>¥ {money(i.amount_fen)}</strong>
                            <dl>
                              <div>
                                <dt>来源</dt>
                                <dd>
                                  {platformName(i.platform)} · {i.order_no}
                                </dd>
                              </div>
                              <div>
                                <dt>归档时间</dt>
                                <dd>{date(i.created_at)}</dd>
                              </div>
                            </dl>
                            <details>
                              <summary>文件信息</summary>
                              <small>{i.filename}</small>
                              <small className="hash">SHA-256 {i.sha256}</small>
                            </details>
                            <button
                              className="button full"
                              onClick={() =>
                                downloadFile(
                                  i.download_url ||
                                    `/api/invoices/${i.id}/file`,
                                  i.filename,
                                )
                              }
                              disabled={busy}
                            >
                              <Icon name="download" size={16} />
                              下载原始文件
                            </button>
                          </article>
                        ))}
                      </div>
                    ) : (
                      <div className="empty">
                        <Icon name="folder" size={42} />
                        <h3>发票仓库还是空的</h3>
                        <p>
                          在订单队列中下载已开具的发票，原始文件会保存在这里。
                        </p>
                        <button
                          className="button"
                          onClick={() => {
                            setView("orders");
                            setPurchaseScope("company");
                            setQueue("P0");
                          }}
                        >
                          查看待下载订单
                        </button>
                      </div>
                    )}
                  </section>
                )}
                {view === "reports" && (
                  <>
                    <div className="report-toolbar">
                      <label>
                        采购月份{" "}
                        <input
                          type="month"
                          value={month}
                          onChange={(e) => setMonth(e.target.value)}
                        />
                      </label>
                      <button
                        className="button primary"
                        disabled={busy || !month}
                        onClick={() =>
                          downloadFile(
                            `/api/export?month=${month}&scope=company&token=${encodeURIComponent(data.token)}`,
                            `${month}_报销准备清单.csv`,
                          )
                        }
                      >
                        <Icon name="download" size={17} />
                        导出报销清单
                      </button>
                    </div>
                    <section className="report-summary">
                      <div>
                        <small>本月已确认公司采购金额</small>
                        <strong>
                          ¥{" "}
                          {money(
                            companyPurchaseAmount(monthOrders),
                          )}
                        </strong>
                        <span>{monthOrders.length} 笔已确认公司采购</span>
                      </div>
                      <div>
                        <small>已归档公司采购金额</small>
                        <strong>
                          ¥{" "}
                          {money(
                            companyPurchaseAmount(monthOrders.filter((o) => o.invoice_status === "downloaded")),
                          )}
                        </strong>
                        <span>{monthInvoices.length} 个原始文件</span>
                      </div>
                      <div>
                        <small>尚待收齐</small>
                        <strong>
                          {
                            monthOrders.filter(
                              (o) =>
                                !["downloaded"].includes(o.invoice_status) &&
                                ["paid", "completed"].includes(o.trade_status),
                            ).length
                          }
                          <em>笔</em>
                        </strong>
                        <span>以实际报销要求核对</span>
                      </div>
                    </section>
                    <section className="panel">
                      <div className="panel-title">
                        <h2>{month} 公司采购与取票明细</h2>
                        <span className="subtle">按订单日期汇总，仅已确认公司采购</span>
                      </div>
                      <p className="scope-filter-note">未纳入或已排除的导入记录不进入报销清单。金额合计仅计已付款、已完成的公司采购；退款和未知交易另待核实。</p>
                      {renderOrders(monthOrders)}
                    </section>
                  </>
                )}
                {view === "settings" && (
                  <div className="settings-grid">
                    <section className="panel profile-panel">
                      <div className="panel-title">
                        <div>
                          <h2>企业开票资料</h2>
                          <p>统一用于两家平台，申请前仍需核对。</p>
                        </div>
                        <span className="privacy-chip">
                          <Icon name="shield" size={14} />
                          本机保存
                        </span>
                      </div>
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          const { configured, ...p } = profile;
                          run(
                            () => api("/profile", p, "PUT"),
                            "企业开票资料已保存",
                          );
                        }}
                      >
                        <div className="form-grid">
                          {(
                            [
                              [
                                "company_name",
                                "公司全称",
                                "与营业执照上的名称一致",
                              ],
                              [
                                "tax_id",
                                "纳税人识别号",
                                "统一社会信用代码 / 税号",
                              ],
                              ["address", "注册地址", "企业注册地址"],
                              ["phone", "注册电话", "企业联系电话"],
                              ["bank_name", "开户银行", "银行及支行名称"],
                              ["bank_account", "银行账号", "企业银行账号"],
                              ["email", "收票邮箱", "用于接收电子发票"],
                            ] as [keyof Profile, string, string][]
                          ).map(([field, label, hint]) => (
                            <label
                              className={
                                field === "company_name" || field === "address"
                                  ? "span-two"
                                  : ""
                              }
                              key={field}
                            >
                              {label}
                              {["company_name", "tax_id"].includes(field) && (
                                <span className="required">*</span>
                              )}
                              <input
                                required={["company_name", "tax_id"].includes(
                                  field,
                                )}
                                autoComplete="off"
                                type={field === "email" ? "email" : "text"}
                                value={String(profile[field] || "")}
                                placeholder={hint}
                                onChange={(e) =>
                                  setProfile({
                                    ...profile,
                                    [field]: e.target.value,
                                  })
                                }
                              />
                            </label>
                          ))}
                        </div>
                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={profile.allow_ordinary}
                            onChange={(e) =>
                              setProfile({
                                ...profile,
                                allow_ordinary: e.target.checked,
                              })
                            }
                          />
                          <span>
                            已确认不支持专票的订单，允许申请普票
                            <small>票种仍未知时，不会自动转成普票。</small>
                          </span>
                        </label>
                        <div className="form-footer">
                          <span>订单和资料只在本机处理</span>
                          <button
                            className="button primary"
                            disabled={busy}
                            type="submit"
                          >
                            保存企业资料
                          </button>
                        </div>
                      </form>
                    </section>
                    <div>
                      <section className="panel connection-panel">
                        <div className="panel-title">
                          <div>
                            <h2>购物账号</h2>
                            <p>使用独立浏览器，由你扫码登录。</p>
                          </div>
                        </div>
                        {(["taobao", "jd"] as Platform[]).map((p) => {
                          const c = data.connections.find(
                            (c) => c.platform === p,
                          );
                          return (
                            <div key={p} className="connection-card">
                              <div className={`connection-logo ${p}`}>
                                {platformName(p).slice(0, 1)}
                              </div>
                              <div>
                                <h3>{platformName(p)}个人账号</h3>
                                <small>
                                  {
                                    (
                                      {
                                        ready: "已连接",
                                        open: "浏览器已打开",
                                        needs_login: "等待登录",
                                        blocked: "需要处理",
                                        disconnected: "尚未连接",
                                      } as Record<string, string>
                                    )[c?.status || "disconnected"]
                                  }
                                </small>
                              </div>
                              <div className="connection-controls">
                                <button
                                  className="button"
                                  disabled={busy}
                                  onClick={() =>
                                    run(
                                      () => api(`/connections/${p}/open`, {}),
                                      "已创建打开浏览器任务，请在窗口中完成登录",
                                    )
                                  }
                                >
                                  打开登录
                                </button>
                                <button
                                  className="button"
                                  disabled={busy}
                                  onClick={() =>
                                    run(
                                      () => api(`/connections/${p}/check`, {}),
                                      "已检查当前登录状态",
                                    )
                                  }
                                >
                                  检查登录
                                </button>
                              </div>
                              <p>{c?.message || "尚未连接购物账号"}</p>
                            </div>
                          );
                        })}
                        <button
                          className="button full"
                          disabled={busy}
                          onClick={() => setScanOpen(true)}
                        >
                          <Icon name="refresh" size={16} />
                          扫描历史订单
                        </button>
                        <button
                          className="text-button close-browser"
                          disabled={busy}
                          onClick={() =>
                            run(
                              () => api("/connections/close", {}),
                              "工具浏览器已关闭，登录状态保留在本机",
                            )
                          }
                        >
                          关闭工具浏览器
                        </button>
                      </section>
                      <section className="panel storage-panel">
                        <h2>本地文件</h2>
                        <p>
                          订单、企业资料和发票保存在本机。请妥善保留数据库、资料密钥和原始发票。
                        </p>
                        <code>{data.data_dir}</code>
                        <button
                          className="text-button"
                          onClick={() => setLogOpen(true)}
                        >
                          查看处理记录 <Icon name="arrow" size={15} />
                        </button>
                      </section>
                    </div>
                  </div>
                )}
              </>
            )}
          </main>
          <footer className="page-footer">
            <span>
              <Icon name="shield" size={13} />
              本地处理，专票优先
            </span>
            <span>平台页面变化或验证时，任务会暂停等待处理</span>
          </footer>
        </div>
        {toast && (
          <div className="toast" role="status">
            <Icon name="check" size={18} />
            {toast}
          </div>
        )}
        {current && (
          <OrderDrawer
            key={`${current.id}-${current.updated_at}`}
            order={current}
            onClose={() => setSelected(null)}
            onSave={(patch) => saveOrder(current, patch)}
            busy={busy}
            action={actionButton(current)}
            onDraft={(kind) => showDraft(current, kind)}
            onReconcile={(outcome, evidence) =>
              run(
                () =>
                  api(`/orders/${encodeURIComponent(current.id)}/reconcile`, {
                    outcome,
                    evidence,
                  }),
                "人工核对结果已保存",
              )
            }
          />
        )}
        {preview && (
          <Modal
            title="核对发票申请"
            onClose={() => setPreview(null)}
          >
            <div className="preview-intro">
              <Icon name="receipt" size={25} />
              <div>
                <h3>{invoiceName(preview.invoice_type)}</h3>
                <p>
                  {liveSubmitPaused ? "请核对订单、票种和企业资料。当前只预览，不提交申请。" : preview.message || "请核对企业资料和订单金额，再确认申请。"}
                </p>
              </div>
            </div>
            <dl className="preview-values preview-target">
              <div>
                <dt>购物平台</dt>
                <dd>
                  <span className={`platform-pill ${preview.platform}`}>
                    {platformName(preview.platform)}
                  </span>
                </dd>
              </div>
              <div>
                <dt>订单编号</dt>
                <dd>{preview.order_no}</dd>
              </div>
              <div>
                <dt>商品名称</dt>
                <dd>{preview.product}</dd>
              </div>
              <div>
                <dt>商家名称</dt>
                <dd>{preview.shop}</dd>
              </div>
            </dl>
            <dl className="preview-values">
              <div>
                <dt>公司抬头</dt>
                <dd>{preview.company_name}</dd>
              </div>
              <div>
                <dt>纳税人识别号</dt>
                <dd>{preview.tax_id}</dd>
              </div>
              <div>
                <dt>订单金额</dt>
                <dd className="money">¥ {money(preview.amount_fen)}</dd>
              </div>
              <div>
                <dt>预览有效至</dt>
                <dd>{date(preview.expires_at)}</dd>
              </div>
            </dl>
            <details className="preview-details">
              <summary>核对补充企业资料（地址、电话与银行）</summary>
              <dl className="preview-values">
                <div>
                  <dt>注册地址</dt>
                  <dd>{preview.address || "未填写"}</dd>
                </div>
                <div>
                  <dt>注册电话</dt>
                  <dd>{preview.phone || "未填写"}</dd>
                </div>
                <div>
                  <dt>开户银行</dt>
                  <dd>{preview.bank_name || "未填写"}</dd>
                </div>
                <div>
                  <dt>银行账号</dt>
                  <dd>{preview.bank_account || "未填写"}</dd>
                </div>
                <div>
                  <dt>收票邮箱</dt>
                  <dd>{preview.email || "未填写"}</dd>
                </div>
              </dl>
            </details>
            <div className="note">
              {liveSubmitPaused
                ? "真实提交已暂停。此处仅供核对本笔申请。"
                : "确认后将尝试在平台提交本笔申请。资料或订单变更后需重新核对，结果不明确时会暂停。"}
            </div>
            <div className="modal-actions">
              <button className="button" onClick={() => setPreview(null)}>
                返回核对
              </button>
              <button
                className="button primary"
                disabled={busy || liveSubmitPaused || preview.workspace !== "live"}
                onClick={() => {
                  if (busy || liveSubmitPaused || preview.workspace !== "live") return;
                  const id = preview.id;
                  run(
                    async () => {
                      await api(
                        `/previews/${encodeURIComponent(id)}/execute`,
                        {},
                      );
                      setPreview(null);
                    },
                    "申请任务已创建，请查看任务结果",
                  );
                }}
              >
                {liveSubmitPaused ? "真实提交已暂停" : "确认提交本笔申请"}
              </button>
            </div>
          </Modal>
        )}
        {draft && (
          <Modal title="商家消息草稿" onClose={() => setDraft(null)}>
            <p className="modal-subtitle">
              核对订单与信息后复制，自行发送给对应商家。
            </p>
            <textarea
              className="draft-area"
              value={draft.text}
              onChange={(e) => setDraft({ ...draft, text: e.target.value })}
            />
            <div className="note">
              工具没有发送这条消息。商家答复后，可在订单详情中记录票种支持情况与依据。
            </div>
            <div className="modal-actions">
              <button className="button" onClick={() => setDraft(null)}>
                关闭
              </button>
              <button
                className="button primary"
                onClick={() => {
                  navigator.clipboard
                    .writeText(draft.text)
                    .then(() =>
                      setToast("草稿已复制，请到对应商家聊天窗口核对后发送"),
                    )
                    .catch(() =>
                      setError("复制失败，请在草稿中手动选择并复制文字。"),
                    );
                }}
              >
                <Icon name="copy" size={16} />
                复制草稿
              </button>
            </div>
          </Modal>
        )}
        {importOpen && (
          <Modal
            title="导入订单"
            onClose={() => setImportOpen(false)}
            wide
          >
            <p className="modal-subtitle">
              支持 UTF-8 CSV 或 JSON。金额字段 amount_fen
              使用整数分；导入不会申请发票。
            </p>
            <div className="import-tools">
              <label className="button file-button">
                <Icon name="plus" size={16} />
                选择文件
                <input
                  type="file"
                  accept=".csv,.json,text/csv,application/json"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (f) {
                      if (f.size > 3 * 1024 * 1024) {
                        setError("文件不能大于 3 MiB");
                        return;
                      }
                      setImportText(await f.text());
                      setImportName(f.name);
                    }
                  }}
                />
              </label>
              <button className="text-button" onClick={template}>
                <Icon name="download" size={16} />
                下载 CSV 模板
              </button>
              <span className="subtle">{importName}</span>
            </div>
            <textarea
              className="import-area"
              aria-label="订单导入内容"
              placeholder="粘贴含表头的 CSV，或输入 JSON 订单数据。"
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
            />
            <div className="note">
              平台值使用 taobao / jd。请核对导入文件中的交易状态（completed
              表示已完成）和发票状态（unrequested
              表示未申请）。公司采购仍需逐单确认；重复订单不会覆盖已有确认。
            </div>
            <div className="modal-actions">
              <button className="button" onClick={() => setImportOpen(false)}>
                取消
              </button>
              <button
                className="button primary"
                disabled={busy || !importText.trim()}
                onClick={importOrders}
              >
                确认导入
              </button>
            </div>
          </Modal>
        )}
        {scanOpen && (
          <Modal title="扫描历史订单" onClose={() => setScanOpen(false)}>
            <p className="modal-subtitle">
              先打开并登录对应购物账号，再扫描指定时间范围。
            </p>
            <div className="form-grid">
              <label className="span-two">
                购物平台
                <select
                  aria-label="购物平台"
                  value={scanPlatform}
                  onChange={(e) => setScanPlatform(e.target.value as Platform)}
                >
                  <option value="taobao">淘宝个人账号</option>
                  <option value="jd">京东个人账号</option>
                </select>
              </label>
              <label>
                起始日期
                <input
                  type="date"
                  value={startDate}
                  max={endDate}
                  onChange={(e) => setStartDate(e.target.value)}
                />
              </label>
              <label>
                结束日期
                <input
                  type="date"
                  value={endDate}
                  min={startDate}
                  onChange={(e) => setEndDate(e.target.value)}
                />
              </label>
              <label>
                最多扫描页数
                <input
                  type="number"
                  min="1"
                  max="100"
                  value={maxPages}
                  onChange={(e) => setMaxPages(Number(e.target.value))}
                />
              </label>
            </div>
            <div className="note">
              扫描只读取订单，不会自动申请。遇到登录失效、验证码或未知页面结构时暂停，扫描任务结果会说明已读取范围。
            </div>
            <div className="modal-actions">
              <button
                className="button"
                disabled={busy}
                onClick={() =>
                  run(
                    () => api(`/connections/${scanPlatform}/open`, {}),
                    "登录窗口任务已创建",
                  )
                }
              >
                先打开登录
              </button>
              <button
                className="button primary"
                disabled={
                  busy ||
                  !startDate ||
                  !endDate ||
                  startDate > endDate ||
                  !Number.isInteger(maxPages) ||
                  maxPages < 1 ||
                  maxPages > 100
                }
                onClick={() =>
                  run(async () => {
                    await api(`/connections/${scanPlatform}/scan`, {
                      start_date: startDate,
                      end_date: endDate,
                      max_pages: maxPages,
                    });
                    setScanOpen(false);
                  }, "订单扫描任务已创建")
                }
              >
                开始扫描
              </button>
            </div>
          </Modal>
        )}
        {logOpen && (
          <Modal title="任务与处理记录" onClose={() => setLogOpen(false)} wide>
            <div className="job-list">
              {data?.jobs.length ? (
                data.jobs.map((j) => (
                  <div key={j.id} className="job-row">
                    <span className={`job-status ${j.status}`}>
                      {(
                        {
                          running: "运行中",
                          done: "已完成",
                          failed: "未完成",
                          needs_attention: "待处理",
                        } as Record<string, string>
                      )[j.status] || j.status}
                    </span>
                    <div>
                      <b>{j.kind}</b>
                      <p>{j.message}</p>
                      <time>{date(j.created_at)}</time>
                    </div>
                  </div>
                ))
              ) : (
                <p className="quiet-text">暂无执行任务。</p>
              )}
            </div>
            <h3 className="log-heading">处理记录</h3>
            <div className="event-list">
              {data?.events.length ? (
                data.events.map((e, i) => (
                  <div className="event-row" key={e.id || i}>
                    <span className="event-dot" />
                    <span>{e.message}</span>
                    <time>{date(e.at)}</time>
                  </div>
                ))
              ) : (
                <p className="quiet-text">暂无处理记录。</p>
              )}
            </div>
          </Modal>
        )}
      </div>
    </ErrorContext.Provider>
  );
}

function OrderDrawer({
  order: o,
  onClose,
  onSave,
  busy,
  action,
  onDraft,
  onReconcile,
}: {
  order: Order;
  onClose: () => void;
  onSave: (p: unknown) => void;
  busy: boolean;
  action: React.ReactNode;
  onDraft: (k: string) => void;
  onReconcile: (outcome: string, evidence: string) => void;
}) {
  const error = React.useContext(ErrorContext);
  const [reconcileEvidence, setReconcileEvidence] = useState("");
  const [purchase, setPurchase] = useState(o.company_purchase);
  const [excluded, setExcluded] = useState(Boolean(o.scope_excluded));
  const [scopeReason, setScopeReason] = useState(o.scope_reason || "");
  const [special, setSpecial] = useState(o.special_status);
  const [ordinary, setOrdinary] = useState(o.ordinary_supported);
  const [evidence, setEvidence] = useState(o.evidence);
  const [tradeStatus, setTradeStatus] = useState(o.trade_status);
  const [invoiceStatus, setInvoiceStatus] = useState(o.invoice_status);
  const [invoiceType, setInvoiceType] = useState(o.invoice_type);
  const [buyer, setBuyer] = useState(o.invoice_buyer || "");
  const [tax, setTax] = useState(o.invoice_tax_id || "");
  const [orderUrl, setOrderUrl] = useState(o.order_url || "");
  const [invoiceUrl, setInvoiceUrl] = useState(o.invoice_url || "");
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className="drawer"
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="drawer-head">
        <div>
          <span className={`platform-pill ${o.platform}`}>
            {platformName(o.platform)}
          </span>
          <span className="subtle">订单详情</span>
        </div>
        <button className="icon-button" aria-label="关闭详情" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className="drawer-body">
        {error && (
          <div className="error-banner" role="alert">
            <p>{error}</p>
          </div>
        )}
        <h2>{o.product}</h2>
        <p className="drawer-shop">{o.shop}</p>
        <div className="drawer-amount">
          ¥ {money(o.amount_fen)}
          <span>{o.paid_at} 订单日期</span>
        </div>
        <dl className="order-meta">
          <div>
            <dt>采购范围</dt>
            <dd>{o.scope_excluded ? "已排除本次取票范围" : isCompanyPurchase(o) ? "已确认公司采购" : "未纳入公司采购"}</dd>
          </div>
          <div>
            <dt>订单编号</dt>
            <dd>{o.order_no}</dd>
          </div>
          <div>
            <dt>交易状态</dt>
            <dd>{statusNames[o.trade_status] || o.trade_status}</dd>
          </div>
          <div>
            <dt>发票状态</dt>
            <dd>{statusNames[o.invoice_status] || o.invoice_status}</dd>
          </div>
          <div>
            <dt>数据来源</dt>
            <dd>{sourceNames[o.source] ?? "本地记录"}</dd>
          </div>
        </dl>
        <div className="decision-box">
          <b>{isCompanyPurchase(o) ? o.decision.label : o.scope_excluded ? "已排除本次取票范围" : "未纳入公司采购"}</b>
          <p>{isCompanyPurchase(o) ? o.decision.reason : o.scope_excluded ? o.scope_reason || "此订单保留来源记录，不进入本次公司取票流程。" : "此订单目前仅作为导入记录保留，不计入公司采购金额，也不会申请公司发票。"}</p>
          {action}
        </div>
        {o.decision.label === "核对提交结果" && (
          <div className="drawer-section">
            <h3>人工核对提交结果</h3>
            <p className="subtle">
              先到平台官方订单详情核对是否已有申请，确认后记录结果。不要在未核对时重新提交。
            </p>
            <label>
              核对依据
              <textarea
                value={reconcileEvidence}
                onChange={(e) => setReconcileEvidence(e.target.value)}
                placeholder="填写官方页面显示的申请状态、核对时间等，至少 8 个字"
              />
            </label>
            <div className="draft-buttons">
              {[
                ["not_submitted", "确认未提交"],
                ["submitted", "确认已提交"],
                ["issued", "确认已开票"],
              ].map(([outcome, label]) => (
                <button
                  className="button"
                  disabled={busy || reconcileEvidence.trim().length < 8}
                  key={outcome}
                  onClick={() => onReconcile(outcome, reconcileEvidence)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="drawer-section">
          <h3>采购范围与开票资格</h3>
          <p className="subtle">只有已核实属于公司的采购才纳入取票。私人消费或本次范围外的订单可直接排除，原始记录会保留。</p>
          <label>
            已核实的交易状态
            <select
              value={tradeStatus}
              onChange={(e) => setTradeStatus(e.target.value)}
            >
              {[
                "unknown",
                "paid",
                "completed",
                "unpaid",
                "closed",
                "refunded",
                "partial_refund",
              ].map((s) => (
                <option value={s} key={s}>
                  {statusNames[s]}
                </option>
              ))}
            </select>
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={purchase}
              onChange={(e) => { setPurchase(e.target.checked); if (e.target.checked) setExcluded(false); }}
            />
            <span>{o.scope_excluded ? "解除本次排除，并纳入已确认公司采购" : "已确认这笔订单属于公司采购"}</span>
          </label>
          <label className="checkbox-row">
            <input type="checkbox" checked={excluded} onChange={(e) => { setExcluded(e.target.checked); if (e.target.checked) setPurchase(false); }}/>
            <span>排除本次开票范围<small>不会申请公司发票，也不计入公司采购和月报。取消排除后仅回到待核对，不会自动成为公司采购。</small></span>
          </label>
          {excluded && <label>排除理由<input value={scopeReason} onChange={(e) => setScopeReason(e.target.value)} placeholder="填写排除这笔订单的理由" maxLength={1000}/></label>}
          <label>
            商家是否支持专票
            <select
              value={special}
              onChange={(e) => setSpecial(e.target.value)}
            >
              <option value="unknown">待确认</option>
              <option value="yes">已确认支持专票</option>
              <option value="no">已确认不支持专票</option>
            </select>
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={ordinary}
              onChange={(e) => setOrdinary(e.target.checked)}
            />
            <span>已确认商家支持公司抬头普票</span>
          </label>
          <label>
            确认依据
            <textarea
              placeholder="填写当前订单页面提示，或商家明确答复及日期"
              value={evidence}
              onChange={(e) => setEvidence(e.target.value)}
            />
          </label>
        </div>
        <details className="drawer-section invoice-correction">
          <summary>核对已开发票信息</summary>
          <p className="subtle">
            仅记录已核实的信息；保存不会修改平台已有发票。
          </p>
          <div className="form-grid">
            <label>
              发票状态
              <select
                value={invoiceStatus}
                onChange={(e) => setInvoiceStatus(e.target.value)}
              >
                {o.invoice_status === "downloaded" && (
                  <option value="downloaded" disabled>
                    已归档（由文件归档确认）
                  </option>
                )}
                {[
                  "unknown",
                  "unrequested",
                  "pending",
                  "issued",
                  "rejected",
                ].map((s) => (
                  <option value={s} key={s}>
                    {statusNames[s]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              票种
              <select
                value={invoiceType}
                onChange={(e) => setInvoiceType(e.target.value)}
              >
                <option value="unknown">未知</option>
                <option value="special">专票</option>
                <option value="ordinary">普票</option>
              </select>
            </label>
            <label className="span-two">
              发票购买方
              <input
                value={buyer}
                onChange={(e) => setBuyer(e.target.value)}
                autoComplete="off"
              />
            </label>
            <label className="span-two">
              发票税号
              <input
                value={tax}
                onChange={(e) => setTax(e.target.value)}
                autoComplete="off"
              />
            </label>
          </div>
        </details>
        <details className="drawer-section invoice-correction">
          <summary>补充官方页面链接</summary>
          <p className="subtle">
            页面无法自动定位时，填写当前订单的官方详情链接。工具会再次核对订单号。
          </p>
          <label>
            订单详情链接
            <input
              type="url"
              value={orderUrl}
              onChange={(e) => setOrderUrl(e.target.value)}
              placeholder="https://"
              autoComplete="off"
            />
          </label>
          <label>
            发票详情链接（可选）
            <input
              type="url"
              value={invoiceUrl}
              onChange={(e) => setInvoiceUrl(e.target.value)}
              placeholder="https://"
              autoComplete="off"
            />
          </label>
        </details>
        {isCompanyPurchase(o) && <div className="drawer-section">
          <h3>联系商家</h3>
          <div className="draft-buttons">
            <button
              className="button"
              disabled={busy}
              onClick={() => onDraft("ask")}
            >
              询问专票草稿
            </button>
            <button
              className="button"
              disabled={busy}
              onClick={() => onDraft("request")}
            >
              申请开票草稿
            </button>
            <button
              className="button"
              disabled={busy}
              onClick={() => onDraft("remind")}
            >
              催票草稿
            </button>
          </div>
        </div>}
        {/^https:\/\//.test(o.order_url) && (
          <a
            className="text-button"
            href={o.order_url}
            target="_blank"
            rel="noreferrer"
          >
            打开平台订单 <Icon name="external" size={15} />
          </a>
        )}
      </div>
      <div className="drawer-footer">
        <button className="button" onClick={onClose}>
          关闭
        </button>
        <button
          className="button primary"
          disabled={busy || (excluded && !scopeReason.trim()) || (purchase && !excluded && special !== "unknown" && !evidence.trim())}
          onClick={() => {
            const patch: Record<string, unknown> = {
              company_purchase: purchase && !excluded,
              scope_excluded: excluded,
              scope_reason: scopeReason,
              special_status: special,
              ordinary_supported: ordinary,
              evidence,
              invoice_type: invoiceType,
              invoice_buyer: buyer,
              invoice_tax_id: tax,
            };
            if (invoiceStatus !== o.invoice_status)
              patch.invoice_status = invoiceStatus;
            if (tradeStatus !== o.trade_status)
              patch.trade_status = tradeStatus;
            if (orderUrl !== o.order_url) patch.order_url = orderUrl;
            if (invoiceUrl !== o.invoice_url) patch.invoice_url = invoiceUrl;
            onSave(patch);
          }}
        >
          {excluded ? "保存范围排除" : o.scope_excluded && purchase ? "解除排除并保存公司采购" : o.scope_excluded ? "解除排除并保留待核对" : "保存核对结果"}
        </button>
      </div>
    </dialog>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
