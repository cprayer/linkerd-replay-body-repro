use linkerd2_proxy_api::{meta, outbound};
use linkerd_app_integration::{controller, policy, proxy};
use std::{env, fs, net::SocketAddr, time::Duration};

fn metadata(kind: &str, name: &str) -> meta::Metadata {
    meta::Metadata {
        kind: Some(meta::metadata::Kind::Resource(meta::Resource {
            group: "gateway.networking.k8s.io".into(),
            kind: kind.into(),
            name: name.into(),
            namespace: "replay".into(),
            ..Default::default()
        })),
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args: Vec<_> = env::args().collect();
    assert_eq!(args.len(), 4, "usage: replay LABEL BACKEND READY_FILE");
    let label = &args[1];
    let target: SocketAddr = args[2].parse().expect("backend address");
    let destination = format!("{label}.test.svc.cluster.local:{}", target.port());
    let ctrl = controller::new();
    let healthy = ctrl.destination_tx(&destination);
    healthy.send_h2_hinted(target);
    let _profile = ctrl.profile_tx_default(target, &format!("{label}.test.svc.cluster.local"));
    let mut senders = vec![healthy];

    let mut route = policy::outbound_default_http_route(&destination);
    route.metadata = Some(metadata("HTTPRoute", label));
    route.rules[0].retry = Some(outbound::http_route::Retry {
        max_retries: 1,
        max_request_bytes: 64 * 1024,
        conditions: Some(outbound::http_route::retry::Conditions {
            status_ranges: vec![outbound::http_route::retry::conditions::StatusRange {
                start: 503,
                end: 503,
            }],
        }),
        timeout: Some(Duration::from_secs(30).try_into().expect("retry timeout")),
        backoff: None,
    });
    if label.starts_with("failfast-") {
        let empty_name = format!("{label}-empty");
        let empty_destination = format!("{empty_name}.test.svc.cluster.local:8080");
        let empty = ctrl.destination_tx(&empty_destination);
        empty.send_no_endpoints();
        senders.push(empty);
        let mut empty_backend = policy::backend(&empty_destination);
        empty_backend.metadata = Some(metadata("Service", &empty_name));
        let backends = [(empty_backend, 100), (policy::backend(&destination), 1)]
            .into_iter()
            .map(
                |(backend, weight)| outbound::http_route::WeightedRouteBackend {
                    backend: Some(outbound::http_route::RouteBackend {
                        backend: Some(backend),
                        ..Default::default()
                    }),
                    weight,
                },
            )
            .collect();
        route.rules[0].backends = Some(outbound::http_route::Distribution {
            kind: Some(outbound::http_route::distribution::Kind::RandomAvailable(
                outbound::http_route::distribution::RandomAvailable { backends },
            )),
        });
    }
    let policies = controller::policy()
        .with_inbound_default(policy::all_unauthenticated())
        .outbound(
            target,
            outbound::OutboundPolicy {
                metadata: Some(metadata("Service", label)),
                protocol: Some(outbound::ProxyProtocol {
                    kind: Some(outbound::proxy_protocol::Kind::Http2(
                        outbound::proxy_protocol::Http2 {
                            routes: vec![route],
                            ..Default::default()
                        },
                    )),
                }),
            },
        );
    let running = proxy::new()
        .controller(ctrl.run().await)
        .policy(policies.run().await)
        .outbound_ip(target)
        .run()
        .await;
    fs::write(
        &args[3],
        serde_json::to_vec(&serde_json::json!({
            "outbound": running.outbound.to_string(),
            "admin": running.admin.to_string(),
        }))
        .expect("serialize listener addresses"),
    )
    .expect("write listener addresses");
    std::future::pending::<()>().await;
    drop((senders, running));
}
