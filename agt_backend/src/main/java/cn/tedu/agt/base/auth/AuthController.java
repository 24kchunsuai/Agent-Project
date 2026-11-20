package cn.tedu.agt.base.auth;

import cn.tedu.agt.base.audit.AuditReporter;
import cn.tedu.agt.base.response.JsonResult;
import com.github.xiaoymin.knife4j.annotations.ApiOperationSupport;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.constraints.NotBlank;
import lombok.Getter;
import lombok.Setter;
import lombok.extern.slf4j.Slf4j;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * 认证接口：登录 / 当前用户 / 登出。
 *
 * <p>账号体系完全代理 jsdstu-3，本系统不存用户表。
 * 前端拿到 token 后放 Authorization: Bearer 头即可。</p>
 */
@Tag(name = "认证模块")
@Slf4j
@Validated
@RestController
@RequestMapping("/v1/auth")
public class AuthController {

    private static final String BEARER_PREFIX = "Bearer ";

    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    @Operation(summary = "登录", description = "账号密码与无人机业务系统（jsdstu-3）一致，如 admin/123456")
    @ApiOperationSupport(order = 10)
    @PostMapping("login")
    public JsonResult login(@Validated @RequestBody LoginParam param, HttpServletRequest request) {
        log.debug("登录:username={}", param.getUsername());
        // IP 只用于审计留痕（E-8）：登录成功/失败都要能回答"从哪来的"
        return JsonResult.ok(authService.login(
                param.getUsername(), param.getPassword(), AuditReporter.clientIp(request)));
    }

    @Operation(summary = "当前登录用户")
    @ApiOperationSupport(order = 20)
    @GetMapping("me")
    public JsonResult me(HttpServletRequest request) {
        return JsonResult.ok(authService.me(extractToken(request)));
    }

    @Operation(summary = "退出登录")
    @ApiOperationSupport(order = 30)
    @PostMapping("logout")
    public JsonResult logout(HttpServletRequest request) {
        authService.logout(extractToken(request), AuditReporter.clientIp(request));
        return JsonResult.ok();
    }

    private String extractToken(HttpServletRequest request) {
        String auth = request.getHeader("Authorization");
        return (auth != null && auth.startsWith(BEARER_PREFIX))
                ? auth.substring(BEARER_PREFIX.length()).trim() : null;
    }

    @Setter
    @Getter
    public static class LoginParam {
        @NotBlank(message = "用户名不能为空")
        private String username;
        @NotBlank(message = "密码不能为空")
        private String password;
    }
}
