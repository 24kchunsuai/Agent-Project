package cn.tedu.agt.base.exception;

import cn.tedu.agt.base.response.JsonResult;
import cn.tedu.agt.base.response.StatusCode;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.client.ResourceAccessException;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 全局异常 → 统一错误码（E-5 响应体组）。
 *
 * <p>"异常不要裸抛成 500" 是规约的硬要求：前端的解包逻辑只认 {@code code}，
 * 一个 500 空响应会让页面白屏而不是弹提示。这里逐条钉住各异常落到哪个码。</p>
 */
class GlobalExceptionHandlerTest {

    private final GlobalExceptionHandler handler = new GlobalExceptionHandler();

    @Test
    @DisplayName("ServiceException 带自定义 message → 用自定义文案（通常是 jsdstu-3 透传的错误原因）")
    void serviceExceptionPrefersItsOwnMessage() {
        JsonResult result = handler.doHandleServiceException(
                new ServiceException(StatusCode.DATA_UNEXISTS, "文档 d-9 不在库中"));

        assertThat(result.getCode()).isEqualTo(3002);
        assertThat(result.getMessage()).isEqualTo("文档 d-9 不在库中");
        assertThat(result.getData()).isNull();
    }

    @Test
    @DisplayName("ServiceException 无 message → 回落到枚举文案（不会返回空白提示）")
    void serviceExceptionFallsBackToEnumMessage() {
        JsonResult result = handler.doHandleServiceException(new ServiceException(StatusCode.UNAUTHORIZED));

        assertThat(result.getCode()).isEqualTo(4010);
        assertThat(result.getMessage()).isEqualTo(StatusCode.UNAUTHORIZED.getMessage());
    }

    @Test
    @DisplayName("确认门异常 → 保留结构化 detail（丢了它前端只能盲点确认）")
    void confirmRequiredKeepsDetail() {
        Map<String, Object> detail = Map.of(
                "requireConfirm", true,
                "expectedDocName", "巡检规范.md",
                "maxSimilarity", 0.93);

        JsonResult result = handler.doHandleConfirmRequiredException(
                new ConfirmRequiredException("请确认是否覆盖现有文档", detail));

        // 状态码固定 3001，与 Python 侧 _kb_fail 的口径一致
        assertThat(result.getCode()).isEqualTo(3001);
        assertThat(result.getMessage()).isEqualTo("请确认是否覆盖现有文档");
        // 这一行是本测试的重点：detail 必须原样出来
        assertThat(result.getData()).isSameAs(detail);
    }

    @Test
    @DisplayName("下游网络不可达 → 3010 智能体不可用（不是 500）")
    void restClientFailureMapsToAgentUnavailable() {
        JsonResult result = handler.doHandleRestClientException(
                new ResourceAccessException("Connection refused"));

        assertThat(result.getCode()).isEqualTo(3010);
        assertThat(result.getCode()).isNotEqualTo(500);
    }

    @Test
    @DisplayName("请求体无法解析 → 3001 参数校验失败（message 是枚举文案，原因在 data）")
    void unreadableBodyMapsToValidateError() {
        // 用三参构造器：单参重载在 Spring 6 已废弃（且它会把 null 当 cause）
        JsonResult result = handler.doHandleHttpMessageNotReadableException(
                new HttpMessageNotReadableException("bad json", null, null));

        // ⚠️ 这里的口径容易看错（本测试第一次跑就栽在这）：JsonResult(StatusCode, data)
        // 的 message **固定取枚举文案**，具体原因放在 **data** 里。
        // 前端解包时 message 给标题、data 给细节，两者分工不能混。
        assertThat(result.getCode()).isEqualTo(3001);
        assertThat(result.getMessage()).isEqualTo(StatusCode.VALIDATE_ERROR.getMessage());
        assertThat(result.getData()).isEqualTo("请求体格式错误");
    }

    @Test
    @DisplayName("兜底 RuntimeException / IllegalArgumentException → 3000（永远不裸抛）")
    void fallbackHandlersNeverThrow() {
        assertThat(handler.doHandleIllegalArgumentException(new IllegalArgumentException("bad arg")).getCode())
                .isEqualTo(3000);
        assertThat(handler.doHandleRuntimeException(new IllegalStateException("boom")).getCode())
                .isEqualTo(3000);
    }
}
