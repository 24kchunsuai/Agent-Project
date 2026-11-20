package cn.tedu.agt.web;

import cn.tedu.agt.agent.client.AgentClient;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.DroneAuthClient;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.base.auth.TokenStore;
import cn.tedu.agt.governance.client.GovernanceClient;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

import java.util.Map;

import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 端到端装配层验证（E-5 鉴权组）。
 *
 * <p>上面那些单元测试证明"每一块砖是对的"，本类证明"砖按对的方式砌在了一起"——
 * 拦截器确实挂在 {@code /**} 上、Controller 路径确实是 {@code /v1/kb/governance/*}、
 * {@code @RestControllerAdvice} 确实把异常转成了 JsonResult（而不是 500 空响应）。
 * 这三件事任何一件错了，单元测试全绿但线上照样坏。</p>
 *
 * <p>用 {@code @MockBean} 把三个出站客户端换掉，配 {@code application-test.properties}
 * 指向不可达端口：任何"忘了 mock 真去连下游"的测试会立刻连接被拒而失败。</p>
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class PermissionEndToEndTest {

    @Autowired
    private MockMvc mockMvc;

    /** 真实使用：登录态解析必须走真家伙，mock 掉就测不到鉴权本身了 */
    @Autowired
    private TokenStore tokenStore;

    @MockBean
    private GovernanceClient governanceClient;

    @MockBean
    private AgentClient agentClient;

    @MockBean
    private DroneAuthClient droneAuthClient;

    private String loginAs(String username, String permission) {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(11L).username(username).permission(permission).build());
        session.setDroneToken("drone-token");
        session.setSessionExpireAt(System.currentTimeMillis() + 60_000);
        return tokenStore.create(session);
    }

    private cn.tedu.agt.agent.pojo.AgentApiResponse<Object> okResponse(Object data) {
        cn.tedu.agt.agent.pojo.AgentApiResponse<Object> r = new cn.tedu.agt.agent.pojo.AgentApiResponse<>();
        r.setCode(2000);
        r.setMessage("操作成功");
        r.setData(data);
        return r;
    }

    // ------------------------------------------------------------ 治理端点：4010 / 4011 / 放行

    @Test
    @DisplayName("治理概览 匿名 → HTTP 200 + code=4010（不是 401/500：前端只认统一响应体）")
    void governanceOverviewAnonymousUnauthorized() throws Exception {
        mockMvc.perform(get("/v1/kb/governance/overview"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(4010));
    }

    @Test
    @DisplayName("治理概览 普通用户 → code=4011（越权与未登录是两个码，前端提示不同）")
    void governanceOverviewNonAdminForbidden() throws Exception {
        String token = loginAs("pilot01", "pilot");

        mockMvc.perform(get("/v1/kb/governance/overview").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(4011));
    }

    @Test
    @DisplayName("治理概览 管理员 → code=2000 且 data 为下游内容")
    void governanceOverviewAdminAllowed() throws Exception {
        when(governanceClient.overview()).thenReturn(okResponse(Map.of("total", 5)));
        String token = loginAs("admin", "admin");

        mockMvc.perform(get("/v1/kb/governance/overview").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(2000))
                .andExpect(jsonPath("$.data.total").value(5));
    }

    @Test
    @DisplayName("传 X-Token 头（8060 的口径）→ 本服务不认，仍判未登录 4010")
    void wrongTokenHeaderIsIgnored() throws Exception {
        String token = loginAs("admin", "admin");

        // 这是最容易误判的场景：头传错了不报错，只表现为"已登录却说未登录"。
        // 有了这条测试，将来若有人"顺手兼容一下 X-Token"，会立刻看到影响面。
        mockMvc.perform(get("/v1/kb/governance/overview").header("X-Token", token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(4010));
    }

    // ------------------------------------------------------------ 统一响应体：异常不裸抛

    @Test
    @DisplayName("登录参数为空 → 3001（走全局异常处理器，不是 400 裸响应）")
    void loginValidationErrorUsesUnifiedBody() throws Exception {
        mockMvc.perform(post("/v1/auth/login")
                        .contentType("application/json")
                        .content("{\"username\":\"\",\"password\":\"\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(3001))
                // 口径：message = 枚举文案（给标题），data = 具体哪个字段错了（给细节）
                .andExpect(jsonPath("$.message").value("参数校验失败"))
                .andExpect(jsonPath("$.data").isNotEmpty());
    }

    @Test
    @DisplayName("/v1/auth/me 未登录 → 4010（拦截器放行 + Service 层拦截，两层协作正确）")
    void meWithoutSessionUnauthorized() throws Exception {
        mockMvc.perform(get("/v1/auth/me"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(4010));
    }

    @Test
    @DisplayName("/v1/auth/me 已登录 → 2000 且不含任何凭据字段")
    void meReturnsSafePayload() throws Exception {
        String token = loginAs("pilot01", "pilot");

        mockMvc.perform(get("/v1/auth/me").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(2000))
                .andExpect(jsonPath("$.data.username").value("pilot01"))
                // drone token / 密码绝对不能下发给前端
                .andExpect(jsonPath("$.data.droneToken").doesNotExist())
                .andExpect(jsonPath("$.data.dronePassword").doesNotExist());
    }

    @Test
    @DisplayName("版本端点匿名可读（部署验收要能在未登录时问版本）")
    void versionEndpointIsAnonymousReadable() throws Exception {
        mockMvc.perform(get("/v1/version"))
                .andExpect(status().isOk());
    }
}
