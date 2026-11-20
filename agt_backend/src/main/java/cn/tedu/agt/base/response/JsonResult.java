package cn.tedu.agt.base.response;

import lombok.AllArgsConstructor;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * 统一响应体，与 drone 项目保持完全一致，前端只需处理一套解包逻辑。
 */
@Setter
@Getter
@NoArgsConstructor
@AllArgsConstructor
public class JsonResult {
    private Integer code;
    private String message;
    private Object data;

    // 针对具有数据的业务结果，创建JsonResult对象
    public JsonResult(StatusCode statusCode, Object data) {
        this.code = statusCode.getCode();
        this.message = statusCode.getMessage();
        this.data = data;
    }

    // 针对无数据的业务结果，创建JsonResult对象
    public JsonResult(StatusCode statusCode) {
        this.code = statusCode.getCode();
        this.message = statusCode.getMessage();
    }

    // 针对成功的业务结果，创建JsonResult对象
    public static JsonResult ok(Object data) {
        return new JsonResult(StatusCode.OPERATION_SUCCESS, data);
    }

    // 针对无数据的成功结果，创建JsonResult对象
    public static JsonResult ok() {
        return ok(null);
    }

    public static JsonResult fail(Object data) {
        return new JsonResult(StatusCode.OPERATION_FAILED, data);
    }
}
