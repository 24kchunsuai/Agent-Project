package cn.tedu.agt.base.auth;

import cn.tedu.agt.base.log.TraceHeaders;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.servlet.HandlerInterceptor;

/**
 * 认证拦截器（<strong>可选认证</strong>模式）：
 * 有合法令牌就把 {@link DroneSession} 挂到请求属性上，没有则放行（匿名）。
 *
 * <p>为什么不强制全站登录？当前对话接口是"公共域 + 受控域"混合体：
 * 知识问答 / 闲聊对所有人开放，业务派活（调 jsdstu-3）才需要身份。
 * 匿名会话走到业务工具时由 Python 侧提示"请先登录"，体验更顺滑。</p>
 *
 * <p>后续若出现必须登录才能碰的接口，在本拦截器加路径白名单或
 * {@code @RequireLogin} 注解即可，本类是唯一的收口点。</p>
 */
@Component
public class AuthInterceptor implements HandlerInterceptor {

    /** 请求属性 key：值为 DroneSession 或 null */
    public static final String ATTR_SESSION = "agt.loginSession";

    private static final String BEARER_PREFIX = "Bearer ";

    private final TokenStore tokenStore;

    public AuthInterceptor(TokenStore tokenStore) {
        this.tokenStore = tokenStore;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) {
        if ("OPTIONS".equalsIgnoreCase(request.getMethod())) {
            return true; // 预检请求不带自定义头，直接放行
        }
        String auth = request.getHeader("Authorization");
        String token = (auth != null && auth.startsWith(BEARER_PREFIX))
                ? auth.substring(BEARER_PREFIX.length()).trim() : null;
        if (StringUtils.hasText(token)) {
            DroneSession session = tokenStore.get(token);
            request.setAttribute(ATTR_SESSION, session);
            // E-6：解析出身份后补写 MDC.userId —— TraceIdFilter 跑在 Filter 层，
            // 那时还不知道是谁，所以身份只能在这里补。日志的 userId 字段由此而来
            // （不是前端传的，前端传什么这里都不看）。
            putMdcUserId(session);
        } else {
            // 匿名请求：必须显式清掉，否则 Tomcat 复用线程时会把上一个请求的用户
            // 记到这个请求头上——"日志里某个人的身份出现在别人的请求里"，最难查的一类错。
            MDC.remove(TraceHeaders.MDC_USER_ID);
        }
        return true;
    }

    private static void putMdcUserId(DroneSession session) {
        if (session != null && session.getUser() != null && session.getUser().getUserId() != null) {
            MDC.put(TraceHeaders.MDC_USER_ID, String.valueOf(session.getUser().getUserId()));
        } else {
            MDC.remove(TraceHeaders.MDC_USER_ID);
        }
    }
}
