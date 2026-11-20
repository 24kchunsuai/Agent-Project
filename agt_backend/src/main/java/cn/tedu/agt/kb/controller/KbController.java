package cn.tedu.agt.kb.controller;

import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.response.JsonResult;
import cn.tedu.agt.kb.pojo.dto.KbUploadParam;
import cn.tedu.agt.kb.service.KbService;
import com.github.xiaoymin.knife4j.annotations.ApiOperationSupport;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.servlet.http.HttpServletRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * 知识库管理接口。
 *
 * <p>与智能体对话链路的共同点：前端一律只访问本服务（8080），
 * 由本层完成鉴权后转发给 Python（8000）——前端不需要知道 Python 的存在，
 * 也不需要处理跨域与 token 透传。owner 在这里从登录态注入，前端传值无效。</p>
 *
 * <p>权限矩阵：</p>
 * <table>
 *   <tr><th>操作</th><th>匿名</th><th>登录用户</th><th>管理员</th></tr>
 *   <tr><td>列表 / 统计 / 检索</td><td>仅 public</td><td>自己的 + public</td><td>全库</td></tr>
 *   <tr><td>上传</td><td>❌ 4010</td><td>✅</td><td>✅</td></tr>
 *   <tr><td>删除（移入回收站）</td><td>❌ 4010</td><td>仅自己上传的</td><td>任意文档</td></tr>
 *   <tr><td>回收站列表</td><td>❌ 4010</td><td>自己删除的</td><td>全库</td></tr>
 *   <tr><td>恢复</td><td>❌ 4010</td><td>仅自己删除的（过 L3 确认门）</td><td>任意文档</td></tr>
 *   <tr><td>彻底删除</td><td>❌ 4010</td><td>❌ 4011（G2 起管理员专属）</td><td>✅ 双确认</td></tr>
 * </table>
 *
 * <p>停用 / 启用 / 对账 / 审计等治理操作见 {@code GovernanceController}（{@code /v1/kb/governance/*}）。</p>
 */
@Tag(name = "知识库管理模块")
@Slf4j
@RestController
@RequestMapping("/v1/kb")
public class KbController {

    /** 会话属性 key，与 AuthInterceptor 约定一致 */
    private static final String ATTR_SESSION = "agt.loginSession";

    @Autowired
    private KbService kbService;

    @Operation(summary = "上传文档并入库",
            description = "支持 .md/.markdown/.txt；同一文件名重复上传为幂等覆盖（先删旧切片再写新的）")
    @ApiOperationSupport(order = 10)
    @PostMapping("documents")
    public JsonResult upload(@Validated @RequestBody KbUploadParam param, HttpServletRequest request) {
        log.debug("知识库上传:fileName={} category={} visibility={}",
                param.getFileName(), param.getCategory(), param.getVisibility());
        return JsonResult.ok(kbService.upload(param, session(request)));
    }

    @Operation(summary = "文档列表",
            description = "按文档聚合展示切片数、分类、归属与可见性；分页参数可选（默认第 1 页、每页 20 条，上限 100）")
    @ApiOperationSupport(order = 20)
    @GetMapping("documents")
    public JsonResult documents(@RequestParam(defaultValue = "1") Integer page,
                                @RequestParam(value = "pageSize", required = false) Integer pageSize,
                                HttpServletRequest request) {
        return JsonResult.ok(kbService.documents(session(request), page, pageSize));
    }

    @Operation(summary = "删除文档", description = "软删除：移入回收站，切片立即从向量库移除（不再被检索），可恢复")
    @ApiOperationSupport(order = 30)
    @DeleteMapping("documents/{docId}")
    public JsonResult delete(@PathVariable String docId, HttpServletRequest request) {
        log.debug("知识库移入回收站:docId={}", docId);
        return JsonResult.ok(kbService.delete(docId, session(request)));
    }

    @Operation(summary = "回收站列表",
            description = "已移入回收站的文档（含删除时间与删除人）；分页参数可选。需登录")
    @ApiOperationSupport(order = 35)
    @GetMapping("recycle")
    public JsonResult recycle(@RequestParam(defaultValue = "1") Integer page,
                              @RequestParam(value = "pageSize", required = false) Integer pageSize,
                              HttpServletRequest request) {
        return JsonResult.ok(kbService.recycled(session(request), page, pageSize));
    }

    @Operation(summary = "从回收站恢复",
            description = "按原切片回灌向量（备份含向量时零 embedding，与删除前逐字节一致）；"
                    + "先过 L3 语义查重确认门：命中相似语料时返回 requireConfirm=true（未执行恢复），"
                    + "用户确认后带 confirm=true 重发")
    @ApiOperationSupport(order = 36)
    @PostMapping("recycle/{docId}/restore")
    public JsonResult restore(@PathVariable String docId,
                              @RequestParam(defaultValue = "false") Boolean confirm,
                              HttpServletRequest request) {
        log.debug("知识库恢复:docId={} confirm={}", docId, confirm);
        return JsonResult.ok(kbService.restore(docId, Boolean.TRUE.equals(confirm), session(request)));
    }

    @Operation(summary = "彻底删除",
            description = "三清（向量 + 备份 + 源文件），不可恢复。**仅管理员**，且需双确认："
                    + "confirm=true 且 confirmDocName 与文档名一致；未满足时返回 requireConfirm=true 与"
                    + " expectedDocName（不执行任何删除）")
    @ApiOperationSupport(order = 37)
    @DeleteMapping("recycle/{docId}")
    public JsonResult purge(@PathVariable String docId,
                            @RequestParam(defaultValue = "false") Boolean confirm,
                            @RequestParam(value = "confirmDocName", required = false) String confirmDocName,
                            HttpServletRequest request) {
        log.debug("知识库彻底删除:docId={} confirm={} 复述名={}", docId, confirm, confirmDocName);
        return JsonResult.ok(kbService.purge(
                docId, Boolean.TRUE.equals(confirm), confirmDocName, session(request)));
    }

    @Operation(summary = "知识库统计", description = "文档数、切片数、分类与可见性分布")
    @ApiOperationSupport(order = 40)
    @GetMapping("stats")
    public JsonResult stats(HttpServletRequest request) {
        return JsonResult.ok(kbService.stats(session(request)));
    }

    @Operation(summary = "检索预览", description = "验证入库内容能否被检索命中（混合检索 + rerank，与问答链路同一检索器）")
    @ApiOperationSupport(order = 50)
    @PostMapping("search")
    public JsonResult search(@RequestBody(required = false) Map<String, String> body, HttpServletRequest request) {
        String query = body == null ? null : body.get("query");
        log.debug("知识库检索预览:query={}", query);
        return JsonResult.ok(kbService.search(query, session(request)));
    }

    /** 取当前会话（可能为 null：本模块允许匿名只读浏览公开内容）。 */
    private DroneSession session(HttpServletRequest request) {
        return (DroneSession) request.getAttribute(ATTR_SESSION);
    }
}
