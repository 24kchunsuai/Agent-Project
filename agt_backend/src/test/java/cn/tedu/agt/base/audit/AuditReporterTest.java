package cn.tedu.agt.base.audit;

import cn.tedu.agt.governance.client.GovernanceClient;
import jakarta.servlet.http.HttpServletRequest;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 审计补录出口（E-8）。
 *
 * <p>这个类只有一条真正重要的性质：<b>它永远不会把异常抛给调用方</b>。
 * 审计是事后能力，不能变成"登录失败"的原因——Python 侧抖一下不该把所有人挡在门外。
 * 所以测试的重心是"失败路径静默"，而不是"成功路径传参对不对"（后者顺带覆盖）。</p>
 */
@ExtendWith(MockitoExtension.class)
class AuditReporterTest {

    @Mock
    private GovernanceClient governanceClient;

    @Mock
    private HttpServletRequest request;

    private AuditReporter reporter() {
        return new AuditReporter(governanceClient);
    }

    // ------------------------------------------------------------ 失败静默

    @Test
    @DisplayName("补录抛异常 → 只吞不抛（否则审计会把登录一起拖挂）")
    void swallowsEveryFailure() {
        when(governanceClient.auditEvent(any(), any(), any(), any(), any(), any()))
                .thenThrow(new RuntimeException("python 挂了"));

        assertThatCode(() -> reporter().loginOk("admin", "10.0.0.1"))
                .doesNotThrowAnyException();
    }

    @Test
    @DisplayName("补录返回 null（Python 侧 500 但被吞成空体）→ 同样不抛")
    void toleratesNullResponse() {
        when(governanceClient.auditEvent(any(), any(), any(), any(), any(), any()))
                .thenReturn(null);

        assertThatCode(() -> reporter().logout("admin", null)).doesNotThrowAnyException();
    }

    // ------------------------------------------------------------ 传参契约

    @Test
    @DisplayName("登录成功：action=LOGIN_OK、target=user:<名>、result=SUCCESS")
    void loginOkCarriesUniformDimensions() {
        reporter().loginOk("admin", "10.0.0.1");

        verify(governanceClient).auditEvent(
                eq("admin"), eq(AuditReporter.LOGIN_OK), eq("user:admin"),
                eq(AuditReporter.RESULT_SUCCESS), isNull(), eq("10.0.0.1"));
    }

    @Test
    @DisplayName("登录失败：result=FAILED 且 detail 带原因")
    void loginFailCarriesReason() {
        reporter().loginFail("admin", "用户名或密码错误", null);

        verify(governanceClient).auditEvent(
                eq("admin"), eq(AuditReporter.LOGIN_FAIL), eq("user:admin"),
                eq(AuditReporter.RESULT_FAILED), eq("用户名或密码错误"), isNull());
    }

    @Test
    @DisplayName("匿名/空用户名 → actor 与 target 归一为 anonymous（审计里没有空操作人）")
    void blankActorFallsBackToAnonymous() {
        reporter().loginFail(null, "用户名或密码错误", null);

        verify(governanceClient).auditEvent(
                eq("anonymous"), eq(AuditReporter.LOGIN_FAIL), eq("user:anonymous"),
                eq(AuditReporter.RESULT_FAILED), eq("用户名或密码错误"), isNull());
    }

    @Test
    @DisplayName("detail 超长截断（Python 侧有长度上限，超了会 422 把审计整条丢掉）")
    void detailIsTruncated() {
        String longDetail = "x".repeat(900);

        reporter().report("admin", AuditReporter.LOGIN_FAIL, "user:admin",
                AuditReporter.RESULT_FAILED, longDetail, null);

        ArgumentCaptor<String> captor = ArgumentCaptor.forClass(String.class);
        verify(governanceClient).auditEvent(any(), any(), any(), any(), captor.capture(), any());
        assertThat(captor.getValue()).hasSizeLessThanOrEqualTo(400);
    }

    // ------------------------------------------------------------ IP 解析

    @Test
    @DisplayName("IP：优先 X-Forwarded-For 第一跳（网关场景拿到的才是真实来源）")
    void prefersForwardedForFirstHop() {
        when(request.getHeader("X-Forwarded-For")).thenReturn("203.0.113.7, 10.0.0.1");

        assertThat(AuditReporter.clientIp(request)).isEqualTo("203.0.113.7");
    }

    @Test
    @DisplayName("IP：无 X-Forwarded-For → 退到 X-Real-IP")
    void fallsBackToRealIp() {
        // 显式把 XFF 钉成 null：Mockito 严格模式下"调用过 getHeader 但换了参数"会报
        // PotentialStubbingProblem，而真实请求里 XFF 就是"有头名、值为空"这一种形态
        when(request.getHeader("X-Forwarded-For")).thenReturn(null);
        when(request.getHeader("X-Real-IP")).thenReturn("198.51.100.9");

        assertThat(AuditReporter.clientIp(request)).isEqualTo("198.51.100.9");
    }

    @Test
    @DisplayName("IP：代理头都没有 → 退到 RemoteAddr")
    void fallsBackToRemoteAddr() {
        when(request.getHeader("X-Forwarded-For")).thenReturn(null);
        when(request.getHeader("X-Real-IP")).thenReturn(null);
        when(request.getRemoteAddr()).thenReturn("127.0.0.1");

        assertThat(AuditReporter.clientIp(request)).isEqualTo("127.0.0.1");
    }

    @Test
    @DisplayName("IP：request 为 null（单元测试/内部调用）→ null，不抛 NPE")
    void nullRequestYieldsNullIp() {
        assertThat(AuditReporter.clientIp(null)).isNull();
    }
}
