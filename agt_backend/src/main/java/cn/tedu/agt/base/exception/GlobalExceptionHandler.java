package cn.tedu.agt.base.exception;

import cn.tedu.agt.base.response.JsonResult;
import cn.tedu.agt.base.response.StatusCode;
import jakarta.validation.ConstraintViolationException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.client.RestClientException;

@Slf4j
@RestControllerAdvice(annotations = RestController.class)
public class GlobalExceptionHandler {

    /**
     * 参数校验失败（@RequestBody + @Validated）
     */
    @ExceptionHandler
    public JsonResult doHandleMethodArgumentNotValidException(MethodArgumentNotValidException ex) {
        String data = ex.getFieldError() == null ? "参数校验失败" : ex.getFieldError().getDefaultMessage();
        log.error("MethodArgumentNotValidException:{}", data);
        return new JsonResult(StatusCode.VALIDATE_ERROR, data);
    }

    /**
     * 参数校验失败（方法参数上的约束注解）
     */
    @ExceptionHandler
    public JsonResult doHandleConstraintViolationException(ConstraintViolationException ex) {
        String data = ex.getMessage();
        log.error("ConstraintViolationException:{}", data);
        return new JsonResult(StatusCode.VALIDATE_ERROR, data);
    }

    /**
     * 请求体无法解析
     */
    @ExceptionHandler
    public JsonResult doHandleHttpMessageNotReadableException(HttpMessageNotReadableException ex) {
        log.error("HttpMessageNotReadableException:{}", ex.getMessage());
        return new JsonResult(StatusCode.VALIDATE_ERROR, "请求体格式错误");
    }

    /**
     * 调用 Python 侧时网络异常：属于外部依赖故障，单独兜底成可读提示
     */
    @ExceptionHandler
    public JsonResult doHandleRestClientException(RestClientException ex) {
        log.error("调用智能体服务失败:{}", ex.getMessage());
        return new JsonResult(StatusCode.AGENT_UNAVAILABLE);
    }

    /**
     * 确认门（G2 语料治理）：与普通业务异常的区别只有一个——<b>保留结构化 detail</b>。
     *
     * <p>前端凭 detail 里的 {@code requireConfirm / expectedDocName / hits} 弹确认框，
     * 丢了它就只能盲点确认。日志用 info 且不打全文：确认门是预期内的正常分支，不是故障。</p>
     */
    @ExceptionHandler
    public JsonResult doHandleConfirmRequiredException(ConfirmRequiredException ex) {
        log.info("确认门拦截:{}", ex.getMessage());
        return new JsonResult(ex.getStatusCode().getCode(), ex.getMessage(), ex.getDetail());
    }

    /**
     * 业务异常。若异常带自定义 message（如 jsdstu-3 透传的错误文案）优先使用。
     */
    @ExceptionHandler
    public JsonResult doHandleServiceException(ServiceException ex) {
        String message = (ex.getMessage() != null && !ex.getMessage().isBlank())
                ? ex.getMessage() : ex.getStatusCode().getMessage();
        log.error("ServiceException:{}", message);
        return new JsonResult(ex.getStatusCode().getCode(), message, null);
    }

    @ExceptionHandler
    public JsonResult doHandleIllegalArgumentException(IllegalArgumentException ex) {
        String data = ex.getMessage();
        log.error("IllegalArgumentException:{}", data);
        return JsonResult.fail(data);
    }

    @ExceptionHandler
    public JsonResult doHandleRuntimeException(RuntimeException ex) {
        String data = ex.getMessage();
        log.error("RuntimeException:{}", data);
        return JsonResult.fail(data);
    }
}
