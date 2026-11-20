package cn.tedu.agt.base.version;

import cn.tedu.agt.base.response.JsonResult;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.io.ClassPathResource;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;

/**
 * 版本与构建信息端点（企业级落地规约 E-1，判据 C2："问他跑的是哪个版本，答得出"）。
 *
 * <p>数据来源是构建期生成的 {@code classpath:build-info.properties}，
 * 由仓库根的 {@code scripts/stamp_version.py} 从 <b>VERSION 文件</b>（唯一版本来源）
 * 加上当时的 git 状态生成。
 *
 * <p>文件不存在时明确返回 {@code unstamped=true}、{@code version=unstamped}，
 * 绝不猜一个像样的版本号——"报未知"只是难看，"编一个版本"会让运维拿着假信息排查。
 * 发布流程（{@code scripts/release.sh}）会强制先打戳再打包。
 *
 * <p>版本一致性由本属性文件保证，<b>不靠 artifact 文件名</b>：{@code pom.xml} 的
 * {@code <finalName>} 已把产物固定为 {@code target/agt_backend.jar}（E-2），
 * Maven 坐标版本（{@code 0.0.1-SNAPSHOT}）与业务版本解耦、不参与部署。
 */
@Slf4j
@RestController
@RequestMapping("/v1/version")
public class VersionController {

    /** 与 scripts/stamp_version.py 的产物路径一致 */
    private static final String BUILD_INFO_RESOURCE = "build-info.properties";

    /** 未打戳时的显式取值 */
    private static final String UNSTAMPED = "unstamped";

    private final Map<String, Object> versionInfo;

    public VersionController() {
        Properties props = loadBuildInfoResource();
        boolean stamped = props.getProperty("build.version") != null;

        Map<String, Object> info = new LinkedHashMap<>();
        info.put("service", "agt_backend");
        info.put("stamped", stamped);
        info.put("version", stamped ? props.getProperty("build.version") : UNSTAMPED);
        info.put("gitCommit", props.getProperty("build.git-commit", UNSTAMPED));
        info.put("gitBranch", props.getProperty("build.git-branch", UNSTAMPED));
        info.put("gitDirty", Boolean.parseBoolean(props.getProperty("build.git-dirty", "false")));
        info.put("buildTime", props.getProperty("build.build-time"));
        // 运行环境：排查"同一版本在不同机器行为不同"时首先要对齐的信息
        info.put("javaVersion", System.getProperty("java.version"));
        info.put("javaVendor", System.getProperty("java.vendor"));

        this.versionInfo = Map.copyOf(info);

        if (stamped) {
            log.info("构建信息：version={} commit={} dirty={} buildTime={}",
                    info.get("version"), info.get("gitCommit"), info.get("gitDirty"),
                    info.get("buildTime"));
        } else {
            log.warn("构建信息缺失（{} 未生成）：/v1/version 将返回 unstamped。"
                            + "正式构建请先执行 `python scripts/stamp_version.py`。",
                    BUILD_INFO_RESOURCE);
        }
    }

    private Properties loadBuildInfoResource() {
        Properties props = new Properties();
        ClassPathResource resource = new ClassPathResource(BUILD_INFO_RESOURCE);
        if (!resource.exists()) {
            return props;
        }
        try (InputStream in = resource.getInputStream()) {
            props.load(in);
        } catch (IOException e) {
            // 版本信息读不出来不该影响服务启动，但必须留痕
            log.warn("读取 {} 失败：{}", BUILD_INFO_RESOURCE, e.getMessage());
        }
        return props;
    }

    @GetMapping
    public JsonResult version() {
        return JsonResult.ok(versionInfo);
    }
}
