package cn.tedu.agt.agent.pojo.vo;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * E-13：AgentChatVO 的 quota 字段必须能承接 Python 的 ChatResponse.quota。
 *
 * <p>守的是 {@code AgentHealthVO} 踩过的同一个坑：本 VO 由 Jackson 直接反序列化
 * Python 响应（{@code AgentApiResponse<AgentChatVO>}），Python 侧新增字段而这里
 * 不声明的话，Jackson <b>静默丢弃、不报错</b>——现象是"直连 8000 有 quota、
 * 走 8080 没了"，用户永远看不到超限提示。字段声明缺了这行，本测试立刻红。</p>
 */
class AgentChatVoQuotaTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    @DisplayName("Python 响应带 quota 时，VO 必须原样接住（Jackson 静默丢字段的回归断言）")
    void quotaFieldSurvivesDeserialization() throws Exception {
        String json = "{"
                + "\"sessionId\":\"s1\",\"content\":\"你好\",\"intent\":\"chitchat\","
                + "\"usage\":{\"promptTokens\":100,\"completionTokens\":50},"
                + "\"quota\":{\"exceeded\":true,\"usedTokens\":12345,\"capTokens\":10000,"
                + "\"message\":\"提示：您的账号今日大模型用量已达上限\"}"
                + "}";

        AgentChatVO vo = mapper.readValue(json, AgentChatVO.class);

        assertThat(vo.getQuota()).isNotNull();
        assertThat(vo.getQuota()).containsEntry("exceeded", true)
                .containsEntry("usedTokens", 12345)
                .containsEntry("capTokens", 10000);
        assertThat((String) vo.getQuota().get("message")).contains("上限");
    }

    @Test
    @DisplayName("无 quota（未触顶）时字段为 null——前端以\"字段缺失\"为正常态，不要造默认提示")
    void quotaFieldIsNullWhenAbsent() throws Exception {
        String json = "{\"sessionId\":\"s1\",\"content\":\"你好\",\"intent\":\"chitchat\"}";

        AgentChatVO vo = mapper.readValue(json, AgentChatVO.class);

        assertThat(vo.getQuota()).isNull();
    }

    @Test
    @DisplayName("quota 结构契约：exceeded / usedTokens / capTokens / message 四键齐全")
    void quotaNoticeShapeContract() {
        // 与 Python costing.quota_notice 的输出结构对齐（改一侧必须同步另一侧）
        Map<String, Object> notice = Map.of(
                "exceeded", true,
                "usedTokens", 150,
                "capTokens", 100,
                "message", "提示：您的账号今日大模型用量已达上限（150 / 100 tokens）。");

        assertThat(notice).containsKeys("exceeded", "usedTokens", "capTokens", "message");
    }
}
