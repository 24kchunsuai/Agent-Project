package cn.tedu.agt.governance.service.impl;

import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.base.exception.ConfirmRequiredException;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.response.StatusCode;
import cn.tedu.agt.governance.client.GovernanceClient;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 治理域权限收口 + 响应拆包（E-5 鉴权组）。
 *
 * <p>这是 Java 层最不能出错的地方之一：治理接口能停用/回滚<b>全体使用者可见的语料</b>。
 * 权限判定写在 Service 层（拦截器是"可选认证"，不负责拦人），
 * 所以测试必须直接打 Service，而不是只测拦截器。</p>
 *
 * <p>三类断言：</p>
 * <ol>
 *   <li><b>越权不下发下游</b>：拒绝必须发生在调 Python <b>之前</b>——
 *       否则越权请求会变成一次真实的写操作 + 一条审计噪音。</li>
 *   <li><b>身份注入取值正确</b>：owner 取登录态、isAdmin 取真实角色，
 *       前端传来的任何值都无效。</li>
 *   <li><b>错误码映射</b>：Python 的业务码 → 本系统码，且确认门的 data 不能丢。</li>
 * </ol>
 */
@ExtendWith(MockitoExtension.class)
class GovernanceServiceImplPermissionTest {

    @Mock
    private GovernanceClient governanceClient;

    @InjectMocks
    private GovernanceServiceImpl service;

    // ------------------------------------------------------------ 测试数据

    private DroneSession sessionOf(String username, String permission) {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(11L).username(username).permission(permission).build());
        session.setSessionExpireAt(System.currentTimeMillis() + 60_000);
        return session;
    }

    private DroneSession anonymous() {
        return null;
    }

    private DroneSession pilot() {
        return sessionOf("pilot01", "pilot");
    }

    private DroneSession admin() {
        return sessionOf("admin", "admin");
    }

    private <T> AgentApiResponse<T> response(Integer code, String message, T data) {
        AgentApiResponse<T> r = new AgentApiResponse<>();
        r.setCode(code);
        r.setMessage(message);
        r.setData(data);
        return r;
    }

    private AgentApiResponse<Object> ok(Object data) {
        return response(2000, "操作成功", data);
    }

    // ------------------------------------------------------------ 只读视图：管理员专属

    @Test
    @DisplayName("匿名访问治理概览 → 4010，且**不下发下游**")
    void anonymousGetsUnauthorized() {
        assertThatThrownBy(() -> service.overview(anonymous()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));

        verifyNoInteractions(governanceClient);
    }

    @Test
    @DisplayName("普通用户（pilot）访问治理概览 → 4011，且不下发下游")
    void nonAdminGetsPermissionDenied() {
        assertThatThrownBy(() -> service.overview(pilot()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.PERMISSION_DENIED));

        verifyNoInteractions(governanceClient);
    }

    @Test
    @DisplayName("管理员访问治理概览 → 放行并返回下游 data")
    void adminIsAllowed() {
        Map<String, Object> data = Map.of("total", 12);
        when(governanceClient.overview()).thenReturn(ok(data));

        assertThat(service.overview(admin())).isSameAs(data);
    }

    @Test
    @DisplayName("角色大小写不敏感（与 jsdstu-3 的 RoleEnum 对齐，避免 ADMIN/admin 之争）")
    void roleCheckIsCaseInsensitive() {
        when(governanceClient.overview()).thenReturn(ok(Map.of()));

        assertThat(service.overview(sessionOf("boss", "ADMIN"))).isNotNull();
    }

    @Test
    @DisplayName("会话存在但用户信息缺失 → 按未登录处理（4010，不误判为越权）")
    void sessionWithoutUserIsTreatedAsAnonymous() {
        DroneSession broken = new DroneSession();
        broken.setSessionExpireAt(System.currentTimeMillis() + 60_000);

        assertThatThrownBy(() -> service.tasks(broken))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));
    }

    // ------------------------------------------------------------ 生命周期：登录即可，管理员范围更大

    @Test
    @DisplayName("停用文档：匿名 → 4010")
    void disableRequiresLogin() {
        assertThatThrownBy(() -> service.disable("d-1", anonymous()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));
        verifyNoInteractions(governanceClient);
    }

    @Test
    @DisplayName("停用文档：普通用户放行，但 owner=登录名 + isAdmin=false（只能停用自己上传的）")
    void disableAsPilotPassesOwnIdentity() {
        when(governanceClient.disable(eq("d-1"), anyString(), anyBoolean())).thenReturn(ok("done"));

        service.disable("d-1", pilot());

        // 用 captor 而非 any()：要证明传下去的是**登录态里的用户名**，
        // 而不是前端可能伪造的字符串。
        ArgumentCaptor<String> owner = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<Boolean> isAdmin = ArgumentCaptor.forClass(Boolean.class);
        verify(governanceClient).disable(eq("d-1"), owner.capture(), isAdmin.capture());

        assertThat(owner.getValue()).isEqualTo("pilot01");
        assertThat(isAdmin.getValue()).isFalse();
    }

    @Test
    @DisplayName("停用文档：管理员 → isAdmin=true（可操作任意文档）")
    void disableAsAdminFlagsIsAdmin() {
        when(governanceClient.disable(eq("d-2"), anyString(), anyBoolean())).thenReturn(ok("done"));

        service.disable("d-2", admin());

        ArgumentCaptor<Boolean> isAdmin = ArgumentCaptor.forClass(Boolean.class);
        verify(governanceClient).disable(eq("d-2"), eq("admin"), isAdmin.capture());
        assertThat(isAdmin.getValue()).isTrue();
    }

    // ------------------------------------------------------------ 早拦：非法枚举不产生一次无效往返

    @Test
    @DisplayName("docs 传非法 state → 3001，且**不调用下游**（省一次往返，也不让参数错与业务拒绝混淆）")
    void invalidStateIsRejectedBeforeCallingDownstream() {
        assertThatThrownBy(() -> service.docs(admin(), "BROKEN_STATE", 1, 20))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.VALIDATE_ERROR));

        verify(governanceClient, never()).docs(any(), anyInt(), anyInt());
    }

    @Test
    @DisplayName("docs 的 state 小写可正常归一（actIve → ACTIVE）")
    void stateIsNormalizedToUpperCase() {
        when(governanceClient.docs(eq("ACTIVE"), anyInt(), anyInt())).thenReturn(ok(Map.of()));

        service.docs(admin(), "actIve", 1, 20);

        verify(governanceClient).docs(eq("ACTIVE"), anyInt(), anyInt());
    }

    // ------------------------------------------------------------ 响应拆包：错误码映射

    @Test
    @DisplayName("Python 返回 3002 → 3002 请求数据不存在")
    void pythonDataUnexistsMaps() {
        when(governanceClient.overview()).thenReturn(response(3002, "文档不存在", null));

        assertThatThrownBy(() -> service.overview(admin()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.DATA_UNEXISTS))
                .hasMessage("文档不存在");
    }

    @Test
    @DisplayName("Python 返回 4010 → 4010（下游说身份无效，本系统照实转达）")
    void pythonUnauthorizedMaps() {
        when(governanceClient.overview()).thenReturn(response(4010, "登录已过期", null));

        assertThatThrownBy(() -> service.overview(admin()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));
    }

    @Test
    @DisplayName("Python 返回 3010 → 3010 智能体不可用")
    void pythonAgentUnavailableMaps() {
        when(governanceClient.overview()).thenReturn(response(3010, "服务不可用", null));

        assertThatThrownBy(() -> service.overview(admin()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.AGENT_UNAVAILABLE));
    }

    @Test
    @DisplayName("未知业务码 → 3000 兜底（不把陌生码原样抛给前端）")
    void unknownPythonCodeFallsBack() {
        when(governanceClient.overview()).thenReturn(response(9999, "谁也不知道这是什么", null));

        assertThatThrownBy(() -> service.overview(admin()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.OPERATION_FAILED))
                // message 仍是下游的原话：码不认识，但原因要如实传达
                .hasMessage("谁也不知道这是什么");
    }

    @Test
    @DisplayName("下游响应体为 null → 3010（不 NPE 裸抛 500）")
    void nullResponseMapsToAgentUnavailable() {
        when(governanceClient.overview()).thenReturn(null);

        assertThatThrownBy(() -> service.overview(admin()))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.AGENT_UNAVAILABLE));
    }

    @Test
    @DisplayName("确认门（3001 + requireConfirm）→ ConfirmRequiredException 且 **detail 一字不丢**")
    void confirmGateKeepsStructuredDetail() {
        Map<String, Object> detail = Map.of(
                "requireConfirm", true,
                "gate", "near_duplicate",
                "expectedDocName", "巡检规范.md",
                "hits", java.util.List.of("巡检规范(副本).md"),
                "maxSimilarity", 0.94);
        when(governanceClient.rollback(eq("d-3"), any(), anyBoolean(), any(), anyString(), anyBoolean()))
                .thenReturn(response(3001, "命中近重复，请确认", detail));

        assertThatThrownBy(() -> service.rollback("d-3", null, true, "巡检规范.md", admin()))
                .isInstanceOf(ConfirmRequiredException.class)
                .satisfies(e -> {
                    ConfirmRequiredException ex = (ConfirmRequiredException) e;
                    assertThat(ex.getStatusCode()).isEqualTo(StatusCode.VALIDATE_ERROR);
                    // 前端凭 detail 弹确认框，丢了它就只能盲点确认
                    assertThat(ex.getDetail()).isSameAs(detail);
                });
    }

    // ------------------------------------------------------------ E-8：操作审计

    @Test
    @DisplayName("审计导出：非管理员在**调用下游之前**被拦（导出=全量证据，不能随便拿）")
    void auditExportRequiresAdmin() {
        // 已登录但非管理员 → 4011 无权限；未登录 → 4010。两者都不是"放过去再被 Python 拒"
        assertThatThrownBy(() -> service.auditExport(pilot(), null, null, null, null, null, null))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.PERMISSION_DENIED));
        assertThatThrownBy(() -> service.auditExport(anonymous(), null, null, null, null, null, null))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));

        verifyNoInteractions(governanceClient);
    }

    @Test
    @DisplayName("审计导出：管理员 → 七个筛选条件**原样下发**（导出必须与页面看到的一致）")
    void auditExportPassesAllFiltersThrough() {
        when(governanceClient.auditExport(any(), any(), any(), any(), any(), any()))
                .thenReturn(response(2000, "ok", Map.of("content", "时间\n", "rows", 1)));

        Object data = service.auditExport(admin(), "admin", "LOGIN_OK", "kb_1",
                "user:", "2026-09-01 00:00:00", "2026-09-30 23:59:59");

        verify(governanceClient).auditExport("admin", "LOGIN_OK", "kb_1",
                "user:", "2026-09-01 00:00:00", "2026-09-30 23:59:59");
        assertThat(data).isInstanceOf(Map.class);
    }

    @Test
    @DisplayName("审计查询：新增的 target 过滤也要原样下发（漏传会让筛选静默失效）")
    void auditQueryPassesTargetThrough() {
        when(governanceClient.audit(any(), any(), any(), any(), any(), any(), anyInt(), anyInt()))
                .thenReturn(response(2000, "ok", Map.of("items", java.util.List.of())));

        service.audit(admin(), null, null, null, "tool:", null, null, 1, 20);

        verify(governanceClient).audit(null, null, null, "tool:", null, null, 1, 20);
    }
}
