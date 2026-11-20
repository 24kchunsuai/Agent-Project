package cn.tedu.agt.base.response;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 统一响应体包装（E-5 响应体组）。
 *
 * <p>{@code JsonResult} 是前端唯一认的解包结构（与 drone 项目同构）。
 * 它的 code/message 一旦漂移，前端要么把所有成功当失败，要么把失败当成功——
 * 这类回归不会有编译错误，只能靠测试钉住。</p>
 */
class JsonResultTest {

    @Test
    @DisplayName("ok(data)：code=2000 + 中文 message + 原样 data")
    void okWithData() {
        Map<String, Object> payload = Map.of("token", "t-1");

        JsonResult result = JsonResult.ok(payload);

        assertThat(result.getCode()).isEqualTo(2000);
        assertThat(result.getMessage()).isEqualTo("操作成功");
        assertThat(result.getData()).isSameAs(payload);
    }

    @Test
    @DisplayName("ok()：无数据成功，data 为 null（前端据此判断不需要解包）")
    void okWithoutData() {
        JsonResult result = JsonResult.ok();

        assertThat(result.getCode()).isEqualTo(2000);
        assertThat(result.getData()).isNull();
    }

    @Test
    @DisplayName("fail(data)：code=3000 + 传入的说明")
    void failCarriesMessage() {
        JsonResult result = JsonResult.fail("路径非法");

        assertThat(result.getCode()).isEqualTo(3000);
        assertThat(result.getMessage()).isEqualTo("操作失败");
        assertThat(result.getData()).isEqualTo("路径非法");
    }

    @Test
    @DisplayName("直接用 StatusCode 构造：code 与 message 都取自枚举（不手写字符串）")
    void constructedFromStatusCode() {
        JsonResult unauthorized = new JsonResult(StatusCode.UNAUTHORIZED);

        assertThat(unauthorized.getCode()).isEqualTo(4010);
        assertThat(unauthorized.getMessage()).isEqualTo(StatusCode.UNAUTHORIZED.getMessage());
        assertThat(unauthorized.getData()).isNull();
    }

    @Test
    @DisplayName("状态码取值锁定：4010/4011 与 jsdstu-3 对齐，3001/3010/3011 与 Python 映射表对齐")
    void statusCodeValuesAreContract() {
        // 这些数字是跨服务契约（前端 + Python + jsdstu-3 三方共用），改动必须是有意识的
        assertThat(StatusCode.OPERATION_SUCCESS.getCode()).isEqualTo(2000);
        assertThat(StatusCode.VALIDATE_ERROR.getCode()).isEqualTo(3001);
        assertThat(StatusCode.DATA_UNEXISTS.getCode()).isEqualTo(3002);
        assertThat(StatusCode.UNAUTHORIZED.getCode()).isEqualTo(4010);
        assertThat(StatusCode.PERMISSION_DENIED.getCode()).isEqualTo(4011);
        assertThat(StatusCode.AGENT_UNAVAILABLE.getCode()).isEqualTo(3010);
        assertThat(StatusCode.AGENT_CALL_ERROR.getCode()).isEqualTo(3011);
    }
}
