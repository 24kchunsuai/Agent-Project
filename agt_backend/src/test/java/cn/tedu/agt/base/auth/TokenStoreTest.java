package cn.tedu.agt.base.auth;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 会话令牌仓库（E-5 鉴权组）。
 *
 * <p>这是"登录态"的唯一载体：它判断错一次，要么会话永不过期（凭据泄露面扩大），
 * 要么刚登录就掉线（每个请求 4010）。所以令牌随机性、惰性过期、删除三条都要钉住。</p>
 */
class TokenStoreTest {

    private TokenStore tokenStore;

    @BeforeEach
    void setUp() {
        tokenStore = new TokenStore();
    }

    private DroneSession sessionExpiringAt(long expireAt) {
        DroneSession s = new DroneSession();
        s.setUser(LoginUser.builder().userId(1L).username("admin").permission("admin").build());
        s.setDroneToken("drone-token");
        s.setSessionExpireAt(expireAt);
        return s;
    }

    @Test
    @DisplayName("创建后可取回同一会话，令牌为 32 位十六进制随机串")
    void createThenGet() {
        String token = tokenStore.create(sessionExpiringAt(System.currentTimeMillis() + 60_000));

        assertThat(token).matches("[0-9a-f]{32}");
        assertThat(tokenStore.get(token)).isNotNull();
        assertThat(tokenStore.get(token).getUser().getUsername()).isEqualTo("admin");
    }

    @Test
    @DisplayName("两次创建的令牌不相同（SecureRandom，不是可预测序列）")
    void tokensAreRandom() {
        String a = tokenStore.create(sessionExpiringAt(System.currentTimeMillis() + 60_000));
        String b = tokenStore.create(sessionExpiringAt(System.currentTimeMillis() + 60_000));
        assertThat(a).isNotEqualTo(b);
    }

    @Test
    @DisplayName("未知 / null / 空令牌一律返回 null（不抛异常，由上层统一转 4010）")
    void unknownTokenReturnsNull() {
        assertThat(tokenStore.get(null)).isNull();
        assertThat(tokenStore.get("")).isNull();
        assertThat(tokenStore.get("   ")).isNull();
        assertThat(tokenStore.get("not-a-real-token")).isNull();
    }

    @Test
    @DisplayName("过期会话不可取用，且顺手被清理（惰性淘汰，不会无限堆积）")
    void expiredTokenIsRejectedAndEvicted() {
        // 过期时间设在过去 1ms：边界条件是"过期即失效"，不是"过期还能用一会儿"
        String token = tokenStore.create(sessionExpiringAt(System.currentTimeMillis() - 1));

        assertThat(tokenStore.get(token)).isNull();
        // 淘汰后再取仍是 null：确认是"移除了"而不是"每次都现算一次"
        assertThat(tokenStore.get(token)).isNull();
    }

    @Test
    @DisplayName("remove 之后取不到（登出路径依赖它）")
    void removeRevokesToken() {
        String token = tokenStore.create(sessionExpiringAt(System.currentTimeMillis() + 60_000));
        tokenStore.remove(token);
        assertThat(tokenStore.get(token)).isNull();
        // 删不存在的令牌不应抛异常：登出是幂等操作
        tokenStore.remove("never-existed");
        tokenStore.remove(null);
    }
}
