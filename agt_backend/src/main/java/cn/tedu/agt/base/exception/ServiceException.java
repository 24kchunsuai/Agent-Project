package cn.tedu.agt.base.exception;

import cn.tedu.agt.base.response.StatusCode;
import lombok.Getter;

/**
 * 自定义业务异常：业务层不能直接返回错误结果，统一抛出本异常由全局异常处理器接管。
 */
public class ServiceException extends RuntimeException {
    @Getter
    private StatusCode statusCode;

    public ServiceException(StatusCode statusCode) {
        this.statusCode = statusCode;
    }

    public ServiceException(StatusCode statusCode, String message) {
        super(message);
        this.statusCode = statusCode;
    }
}
