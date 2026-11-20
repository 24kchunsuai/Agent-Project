package cn.tedu.agt.base.config;

import cn.tedu.agt.base.auth.AuthInterceptor;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * SpringMVC 跨域配置 + 认证拦截器注册。
 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    private final AuthInterceptor authInterceptor;

    public WebConfig(AuthInterceptor authInterceptor) {
        this.authInterceptor = authInterceptor;
    }

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/**")                       // 匹配任意请求路径
                .allowedHeaders("*")                     // 允许任意请求头
                .allowedMethods("*")                     // 允许任意请求方法
                .allowedOriginPatterns("*")              // 允许任意域名访问
                .allowCredentials(true)                  // 允许携带 cookie/session
                .maxAge(3600);                           // 预检结果缓存一小时
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        // 可选认证：有令牌就解析身份，无令牌放行（业务派活时由智能体侧提示登录）
        registry.addInterceptor(authInterceptor).addPathPatterns("/**");
    }
}
