package cn.tedu.agt.base.auth;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.security.SecureRandom;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 会话令牌仓库：agt_token -> DroneSession。
 *
 * <p>与 jsdstu-3 的 TokenStore 同构：不透明随机令牌 + 内存表 + 惰性过期。
 * 令牌 32 位十六进制随机串，无 JWT 依赖；单实例演示期够用，
 * 多实例时升级 Redis 只需替换本类实现。</p>
 */
@Slf4j
@Component
public class TokenStore {

    private static final SecureRandom RANDOM = new SecureRandom();

    private final Map<String, DroneSession> store = new ConcurrentHashMap<>();

    /** 创建会话，返回 agt 令牌 */
    public String create(DroneSession session) {
        String token = newToken();
        store.put(token, session);
        return token;
    }

    /**
     * 取会话：不存在或已过期返回 null（过期顺手清理，惰性淘汰）。
     */
    public DroneSession get(String token) {
        if (token == null || token.isBlank()) {
            return null;
        }
        DroneSession session = store.get(token);
        if (session == null) {
            return null;
        }
        if (System.currentTimeMillis() > session.getSessionExpireAt()) {
            store.remove(token);
            log.info("会话已过期，予以清除:userId={}", session.getUser() != null ? session.getUser().getUserId() : null);
            return null;
        }
        return session;
    }

    public void remove(String token) {
        if (token != null) {
            store.remove(token);
        }
    }

    private String newToken() {
        byte[] bytes = new byte[16];
        RANDOM.nextBytes(bytes);
        StringBuilder sb = new StringBuilder(32);
        for (byte b : bytes) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }
}
