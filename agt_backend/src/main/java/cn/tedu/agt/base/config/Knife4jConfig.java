package cn.tedu.agt.base.config;

import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Contact;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.tags.Tag;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.Arrays;
import java.util.List;

@Configuration
public class Knife4jConfig {

    @Bean
    public OpenAPI customOpenAPI() {
        return new OpenAPI().info(getApiInfo()).tags(getTagsOrdered());
    }

    // 定义API的基本信息
    private Info getApiInfo() {
        return new Info()
                .title("Drone Agent API DOC")
                .description("无人机智能体系统接口文档")
                .version("1.0.0")
                .contact(new Contact().name("LBB"));
    }

    // 定义tag顺序
    private List<Tag> getTagsOrdered() {
        return Arrays.asList(
                new Tag().name("智能体模块").description("智能体对话相关接口"),
                new Tag().name("系统模块").description("系统信息与探针接口")
        );
    }
}
