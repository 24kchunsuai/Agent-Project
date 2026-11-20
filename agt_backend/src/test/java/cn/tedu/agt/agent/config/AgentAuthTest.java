package cn.tedu.agt.agent.config;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * S-1 服务间信任边界的**最内层守卫**（E-5 透传契约组）。
 *
 * <p>这个类只有 60 行，却是整个 S-1 的收口：三个 Java 出口
 * （{@code AgentClient} / {@code KbClient} / {@code GovernanceClient}）都从这里取密钥。
 * 它错一次，前端看到的是"每个操作都弹登录已过期"，排查方向会被带到会话上去——
 * 所以这三点必须有测试钉住。</p>
 */
class AgentAuthTest {

    private static AgentProperties propsWithKey(String key) {
        AgentProperties p = new AgentProperties();
        p.setSharedKey(key);
        return p;
    }

    @Test
    @DisplayName("密钥为空 → 抛异常（fail-closed，不允许静默降级为不带头发请求）")
    void blankKeyFailsFast() {
        // null / 空串 / 全空白三种"没配"的常见形态都要挡住。
        // 若这里放行，症状会变成"每个请求都拿到 Python 的 4010"——
        // 一次配置错误被伪装成会话问题，排查成本远高于启动即失败。
        for (String blank : new String[]{null, "", "   "}) {
            assertThatThrownBy(() -> AgentAuth.requireSharedKey(propsWithKey(blank)))
                    .as("密钥为 [%s] 时必须拒绝", blank)
                    .isInstanceOf(IllegalStateException.class)
                    .hasMessageContaining("AGENT_SHARED_KEY");
        }
    }

    @Test
    @DisplayName("密钥已配 → 原样返回（不做任何 trim/转换，两侧必须逐字节相同）")
    void keyReturnedVerbatim() {
        String key = "AbC-123_xyz";
        assertThat(AgentAuth.requireSharedKey(propsWithKey(key))).isEqualTo(key);
    }

    @Test
    @DisplayName("请求头名固定为 X-Agent-Key（与 Python 侧 auth.HEADER_NAME 同值）")
    void headerNameIsContract() {
        // 头名是两个服务之间唯一的约定；改了它 Python 侧会 4010，
        // 而报错信息只会说"未授权"，不会说"头名对不上"。
        assertThat(AgentAuth.HEADER_NAME).isEqualTo("X-Agent-Key");
    }

    @Test
    @DisplayName("日志掩码不泄露完整密钥，但保留长度信息便于核对两侧是否同值")
    void maskHidesSecret() {
        String key = "abcdefghijklmnop";
        String masked = AgentAuth.mask(key);

        assertThat(masked).doesNotContain(key);
        assertThat(masked).contains("len=16");
        // 短密钥一律打全掩码：留头尾反而会暴露大部分内容
        assertThat(AgentAuth.mask("abcd")).isEqualTo("****");
        assertThat(AgentAuth.mask(null)).isEqualTo("****");
    }
}
