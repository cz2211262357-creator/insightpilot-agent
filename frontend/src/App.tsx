import { useEffect, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Navigate,
  NavLink,
  Route,
  Routes,
  useNavigate,
} from "react-router-dom";
import {
  Compass,
  ChartNoAxesCombined,
  History,
  BookOpen,
  UsersRound,
  LogOut,
  ShieldCheck,
  ArrowRight,
  Database,
  CheckCircle2,
} from "lucide-react";
import {
  api,
  errorText,
  onExpired,
  post,
  resetSession,
  sessionGeneration,
} from "./api/client";
import type { Actor, Catalog, Member, System } from "./api/types";
import { Loading } from "./components/Common";
import { Workbench } from "./workbench/Workbench";
import { HistoryPage } from "./history/HistoryPage";
import { CatalogPage } from "./catalog/CatalogPage";
import { MembersPage } from "./auth/MembersPage";

function Login({ onLogin }: { onLogin: (actor: Actor) => void }) {
  const [tenant, setTenant] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function login(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const actor = await post<Actor>("/auth/login", {
        tenant_id: tenant.trim(),
        email: email.trim(),
        password,
      });
      setPassword("");
      onLogin(actor);
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <div className="login-card">
        <section className="login-story">
          <div className="login-brand">
            <Compass size={28} />
            <span>InsightPilot</span>
          </div>
          <span className="eyebrow">BUSINESS INSIGHTS, WITH EVIDENCE</span>
          <h1>
            把业务问题，
            <br />
            变成有据可查的答案。
          </h1>
          <p>
            统一指标口径，按权限查询数据，
            <br />
            让每次分析都留下清楚的依据。
          </p>
          <div className="login-steps">
            <div>
              <span>01</span>
              <strong>确认你的问题</strong>
              <small>指标、时间范围与筛选条件</small>
            </div>
            <div>
              <span>02</span>
              <strong>在权限内分析</strong>
              <small>只读执行，必要时由你确认</small>
            </div>
            <div>
              <span>03</span>
              <strong>交付证据报告</strong>
              <small>数值、查询与数据版本相互对应</small>
            </div>
          </div>
          <footer>
            <ShieldCheck size={15} />
            只读分析 · 明确确认 · 可追溯证据
          </footer>
        </section>
        <section className="login-form-area">
          <span className="login-mark">
            <Compass size={25} />
          </span>
          <h2>登录分析工作台</h2>
          <p className="muted">使用管理员为你配置的租户与成员账号。</p>
          <form onSubmit={(event) => void login(event)}>
            <label htmlFor="tenant">租户 ID</label>
            <input
              id="tenant"
              aria-label="租户 ID"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              required
              placeholder="输入租户 UUID"
              autoComplete="organization"
              maxLength={36}
            />
            <label htmlFor="email">邮箱</label>
            <input
              id="email"
              aria-label="邮箱"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              required
              autoComplete="username"
              placeholder="name@example.com"
            />
            <label htmlFor="password">密码</label>
            <input
              id="password"
              aria-label="密码"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              type="password"
              required
              autoComplete="current-password"
              placeholder="输入你的密码"
              maxLength={256}
            />
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            <button className="button primary login-submit" disabled={busy}>
              {busy ? "正在登录" : "进入工作台"}
              <ArrowRight size={16} />
            </button>
          </form>
          <div className="login-data-note">
            <Database size={16} />
            <span>
              初始经营数据为明确标注的虚构数据，分析通过真实模型与查询链路执行。
            </span>
          </div>
          <small className="login-copyright">
            InsightPilot · 智能问数与经营分析
          </small>
        </section>
      </div>
    </main>
  );
}

function Shell({ actor, onLogout }: { actor: Actor; onLogout: () => void }) {
  const [error, setError] = useState("");
  const [leaving, setLeaving] = useState(false);
  const profile = useQuery({
    queryKey: ["self-profile"],
    queryFn: () => api<Member>(`/members/${actor.user_id}`),
  });
  const catalog = useQuery({
    queryKey: ["catalog"],
    queryFn: () => api<Catalog>("/datasets"),
    staleTime: 30000,
  });
  const system = useQuery({
    queryKey: ["system"],
    queryFn: () => api<System>("/system"),
    staleTime: 60000,
  });
  useQuery({
    queryKey: ["session-check"],
    queryFn: () => api<Actor>("/auth/me"),
    refetchInterval: 15000,
  });
  async function logout() {
    setLeaving(true);
    try {
      await post("/auth/logout", {});
      onLogout();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setLeaving(false);
    }
  }
  return (
    <div className="shell">
      <aside className="sidebar">
        <a href="/app/workbench" className="brand">
          <span>
            <Compass size={25} />
          </span>
          <div>
            <strong>InsightPilot</strong>
            <small>经营分析工作台</small>
          </div>
        </a>
        <span className="nav-label">工作空间</span>
        <nav>
          <NavLink to="/app/workbench">
            <ChartNoAxesCombined size={19} />
            问数工作台
          </NavLink>
          <NavLink to="/app/history">
            <History size={19} />
            分析记录
          </NavLink>
          <NavLink to="/app/catalog">
            <BookOpen size={19} />
            指标目录
          </NavLink>
          {actor.role === "admin" && (
            <>
              <span className="nav-label">管理</span>
              <NavLink to="/app/members">
                <UsersRound size={19} />
                成员管理
              </NavLink>
            </>
          )}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-dataset">
            <span className="dataset-dot" />
            <strong>虚构经营数据</strong>
            <p>
              {catalog.data?.as_of
                ? "数据快照 " +
                  new Date(catalog.data.as_of).toLocaleDateString("zh-CN", {
                    timeZone: "Asia/Shanghai",
                  })
                : "按当前指标目录分析"}
            </p>
            <NavLink to="/app/catalog">
              查看数据与口径
              <ArrowRight size={12} />
            </NavLink>
          </div>
          <div className="sidebar-user">
            <span className="avatar">
              {(profile.data?.display_name ?? "成员").slice(0, 1)}
            </span>
            <div>
              <strong>{profile.data?.display_name ?? "已登录成员"}</strong>
              <small>{actor.role === "admin" ? "管理员" : "分析员"}</small>
            </div>
            <button
              title="退出登录"
              aria-label="退出登录"
              disabled={leaving}
              onClick={() => void logout()}
            >
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>
      <div className="shell-content">
        <header className="topbar">
          <span className="breadcrumb">
            工作空间<span>/</span>经营分析
          </span>
          <div>
            <span className="readonly-badge">
              <ShieldCheck size={14} />
              只读分析
            </span>
            <span className="model-badge">
              <CheckCircle2 size={14} />
              {system.data?.model_name ?? "读取模型配置"}
            </span>
            <button
              className="mobile-logout"
              aria-label="退出登录"
              disabled={leaving}
              onClick={() => void logout()}
            >
              <LogOut size={17} />
            </button>
          </div>
        </header>
        <main className="main-content">
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          {catalog.error && (
            <p className="notice error">{errorText(catalog.error)}</p>
          )}
          <Routes>
            <Route
              path="/app/workbench"
              element={
                <Workbench catalog={catalog.data} system={system.data} />
              }
            />
            <Route
              path="/app/history"
              element={<HistoryPage catalog={catalog.data} />}
            />
            <Route
              path="/app/catalog"
              element={<CatalogPage catalog={catalog.data} />}
            />
            {actor.role === "admin" && (
              <Route
                path="/app/members"
                element={<MembersPage actor={actor} />}
              />
            )}
            <Route
              path="*"
              element={<Navigate to="/app/workbench" replace />}
            />
          </Routes>
        </main>
        <footer className="app-footer">
          InsightPilot · 每份报告保留查询证据与数据版本
        </footer>
      </div>
    </div>
  );
}

export default function App() {
  const [actor, setActor] = useState<Actor | null>(null);
  const [checking, setChecking] = useState(true);
  const client = useQueryClient();
  const navigate = useNavigate();
  useEffect(() => {
    const dispose = onExpired(() => {
      setActor(null);
      setChecking(false);
      client.clear();
    });
    let active = true;
    void api<Actor>("/auth/me")
      .then((user) => {
        if (active) setActor(user);
      })
      .catch(() => {
        if (active) setActor(null);
      })
      .finally(() => {
        if (active) setChecking(false);
      });
    return () => {
      active = false;
      dispose();
    };
  }, [client]);
  function logout() {
    resetSession();
    client.clear();
    setActor(null);
    navigate("/login", { replace: true });
  }
  if (checking)
    return (
      <div className="app-loading">
        <Compass size={32} />
        <Loading text="正在检查登录状态" />
      </div>
    );
  return actor ? (
    <Shell
      key={actor.tenant_id + actor.user_id + sessionGeneration()}
      actor={actor}
      onLogout={logout}
    />
  ) : (
    <Login
      onLogin={(user) => {
        client.clear();
        setActor(user);
        navigate("/app/workbench", { replace: true });
      }}
    />
  );
}
