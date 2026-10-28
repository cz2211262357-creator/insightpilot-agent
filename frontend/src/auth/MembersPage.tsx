import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { UsersRound } from "lucide-react";
import { api } from "../api/client";
import type { Actor, Member } from "../api/types";
import { Empty, ErrorNotice, Loading, PageTitle } from "../components/Common";
export function MembersPage({ actor }: { actor: Actor }) {
  const query = useQuery({
    queryKey: ["members"],
    queryFn: () => api<Member[]>("/members"),
  });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<unknown>();
  async function update(
    member: Member,
    patch: { role?: "admin" | "analyst"; enabled?: boolean },
  ) {
    setBusy(member.user_id);
    setError(undefined);
    try {
      await api<Member>(`/members/${member.user_id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await query.refetch();
    } catch (error) {
      setError(error);
    } finally {
      setBusy("");
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="ACCESS MANAGEMENT"
        title="成员管理"
        description="成员权限变更后，旧登录与正在运行的授权会重新核验。"
      />
      <section className="panel">
        <div className="section-heading">
          <h2>
            <UsersRound size={18} />
            当前租户成员
          </h2>
          <span className="muted tiny">最多显示最近 100 名成员</span>
        </div>
        {error !== undefined && <ErrorNotice error={error} />}{" "}
        {query.isLoading ? (
          <Loading />
        ) : query.error ? (
          <ErrorNotice error={query.error} retry={() => void query.refetch()} />
        ) : !query.data?.length ? (
          <Empty title="暂无成员" />
        ) : (
          <div className="table-scroll">
            <table className="members-table">
              <thead>
                <tr>
                  <th>成员</th>
                  <th>角色</th>
                  <th>访问状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {query.data.map((member) => (
                  <tr key={member.user_id}>
                    <td>
                      <strong>{member.display_name}</strong>
                      {member.user_id === actor.user_id && (
                        <span className="tag">当前账号</span>
                      )}
                      <small>{member.email}</small>
                    </td>
                    <td>
                      <select
                        aria-label={"角色 " + member.email}
                        value={member.role}
                        disabled={
                          Boolean(busy) || member.user_id === actor.user_id
                        }
                        onChange={(e) =>
                          void update(member, {
                            role: e.target.value as "admin" | "analyst",
                          })
                        }
                      >
                        <option value="admin">管理员</option>
                        <option value="analyst">分析员</option>
                      </select>
                    </td>
                    <td>
                      <span
                        className={
                          "access-state " +
                          (member.enabled ? "enabled" : "disabled")
                        }
                      >
                        {member.enabled ? "已启用" : "已停用"}
                      </span>
                    </td>
                    <td>
                      <button
                        className="button secondary small"
                        disabled={
                          Boolean(busy) || member.user_id === actor.user_id
                        }
                        onClick={() =>
                          void update(member, { enabled: !member.enabled })
                        }
                      >
                        {busy === member.user_id
                          ? "保存中"
                          : member.enabled
                            ? "停用访问"
                            : "启用访问"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted tiny member-note">
          当前账号在此页保持可用；成员列表不包含密码或访问令牌。
        </p>
      </section>
    </>
  );
}
