package cn.tedu.agt.base.auth;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.util.Map;

/**
 * 访问 jsdstu-3（无人机业务后端）认证接口的唯一出口。
 *
 * <p>当前只用登录/登出两个接口；后续业务工具调用统一走 Python 侧
 * JsDstuClient（携带用户自己的 token），Java 侧不做业务转发。</p>
 */
@Slf4j
@Component
public class DroneAuthClient {

    private static final String LOGIN_PATH = "/v1/user/login";
    private static final String LOGOUT_PATH = "/v1/user/logout";

    private final RestClient restClient;

    public DroneAuthClient(@Value("${drone.base-url:http://localhost:8060}") String baseUrl) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(3000);
        factory.setReadTimeout(10000);
        this.restClient = RestClient.builder()
                .baseUrl(baseUrl)
                .requestFactory(factory)
                .build();
        log.info("jsdstu-3 认证客户端已初始化:baseUrl={}", baseUrl);
    }

    /**
     * 登录：透传 jsdstu-3 的 JsonResult 结构（code/message/data），
     * 登录失败（3004/3005 等）由调用方按 code 原样返回前端。
     */
    public Map<String, Object> login(String username, String password) {
        return restClient.post()
                .uri(LOGIN_PATH)
                .header("Content-Type", "application/json")
                .body(Map.of("username", username, "password", password))
                .retrieve()
                .body(new ParameterizedTypeReference<Map<String, Object>>() {
                });
    }

    /** 登出：尽力而为，失败不抛异常（本地会话照样删除） */
    public void logout(String droneToken) {
        try {
            restClient.post()
                    .uri(LOGOUT_PATH)
                    .header("Authorization", "Bearer " + droneToken)
                    .retrieve()
                    .toBodilessEntity();
        } catch (Exception e) {
            log.warn("jsdstu-3 登出调用失败（忽略）:{}", e.getMessage());
        }
    }
}
