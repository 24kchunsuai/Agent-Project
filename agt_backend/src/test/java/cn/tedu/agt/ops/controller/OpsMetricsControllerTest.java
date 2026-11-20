package cn.tedu.agt.ops.controller;

import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.ops.service.OpsService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;

/**
 * {@code GET /v1/ops/metrics} 的 HTTP 层契约（E-9）。
 *
 * <p>本层要守的是"<b>文本没被包装、Content-Type 带版本号</b>"这两件事，
 * 它们恰好是 Java 侧做 Prometheus 端点最容易翻车的地方：</p>
 * <ul>
 *   <li>本项目所有接口都返回 {@code JsonResult}（{@code {code,data,message}}）。
 *       指标端点如果顺手用了它，Prometheus 会拿到一坨 JSON 并<b>直接判定抓取失败</b>——
 *       而人用浏览器打开却"看着正常"（有内容、HTTP 200）。</li>
 *   <li>{@code text/plain} 不带 {@code version=0.0.4} 时，抓取器无法确定用哪个解析器；
 *       这是 Prometheus 文档明确要求的，漏了在很多版本上会直接报
 *       {@code unsupported Content-Type}。</li>
 * </ul>
 */
class OpsMetricsControllerTest {

    private MockMvc mvc;
    private final AtomicReference<Integer> seenWindow = new AtomicReference<>();
    private final AtomicReference<Boolean> seenFresh = new AtomicReference<>();

    @BeforeEach
    void setUp() {
        OpsService stub = new OpsService() {
            @Override
            public Map<String, Object> health(boolean fresh) {
                return Map.of("status", "up");
            }

            @Override
            public String metrics(int windowHours, boolean fresh) {
                seenWindow.set(windowHours);
                seenFresh.set(fresh);
                return "# HELP agt_java_up x\n# TYPE agt_java_up gauge\nagt_java_up 1\n";
            }

            @Override
            public Map<String, Object> clientError(DroneSession session, Map<String, Object> body) {
                // 本测试类只验指标端点；E-12 的上报契约另有专项测试
                // （OpsClientErrorControllerTest）。桩不返回"成功"，
                // 免得将来有人误以为这里验过上报。
                return Map.of("accepted", false, "stored", false, "reason", "not stubbed here");
            }
        };
        OpsController controller = new OpsController();
        ReflectionTestUtils.setField(controller, "opsService", stub);
        mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    @Test
    @DisplayName("返回 text/plain 且带 version=0.0.4")
    void returnsPrometheusContentType() throws Exception {
        var resp = mvc.perform(get("/v1/ops/metrics")).andReturn().getResponse();
        assertThat(resp.getStatus()).isEqualTo(200);
        assertThat(resp.getContentType()).contains("text/plain");
        assertThat(resp.getContentType()).contains("version=0.0.4");
        assertThat(resp.getContentAsString()).startsWith("# HELP agt_java_up");
    }

    @Test
    @DisplayName("响应体就是纯文本，没有 {code,data} 外壳")
    void bodyIsRawTextNotJsonEnvelope() throws Exception {
        String body = mvc.perform(get("/v1/ops/metrics")).andReturn().getResponse().getContentAsString();
        assertThat(body.stripLeading()).doesNotStartWith("{");
        assertThat(body).doesNotContain("\"code\"");
    }

    @Test
    @DisplayName("默认 window=24、fresh=false")
    void defaultsAreSane() throws Exception {
        mvc.perform(get("/v1/ops/metrics"));
        assertThat(seenWindow.get()).isEqualTo(24);
        assertThat(seenFresh.get()).isFalse();
    }

    @Test
    @DisplayName("window / fresh 参数被透传")
    void paramsAreForwarded() throws Exception {
        mvc.perform(get("/v1/ops/metrics").param("window", "6").param("fresh", "true"));
        assertThat(seenWindow.get()).isEqualTo(6);
        assertThat(seenFresh.get()).isTrue();
    }
}
