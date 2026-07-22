package manager

import (
	"context"
	"fmt"
	"net"
	"sort"
	"strings"
)

type Resolver interface {
	LookupIP(context.Context, string, string) ([]net.IP, error)
}

type systemResolver struct{}

func (systemResolver) LookupIP(ctx context.Context, network, host string) ([]net.IP, error) {
	return net.DefaultResolver.LookupIP(ctx, network, host)
}

var builtInDeniedCIDRs = []string{
	"0.0.0.0/8",
	"127.0.0.0/8",
	"169.254.0.0/16",
	"224.0.0.0/4",
	"::/128",
	"::1/128",
	"fe80::/10",
}

func normalizeScope(scope Scope) (Scope, error) {
	for _, cidr := range append(append([]string{}, scope.CIDRs...), scope.DeniedCIDRs...) {
		if _, _, err := net.ParseCIDR(cidr); err != nil {
			return Scope{}, fmt.Errorf("invalid CIDR %q: %w", cidr, err)
		}
	}
	denied := make(map[string]struct{}, len(scope.DeniedCIDRs)+len(builtInDeniedCIDRs))
	for _, cidr := range append(scope.DeniedCIDRs, builtInDeniedCIDRs...) {
		denied[cidr] = struct{}{}
	}
	scope.DeniedCIDRs = scope.DeniedCIDRs[:0]
	for cidr := range denied {
		scope.DeniedCIDRs = append(scope.DeniedCIDRs, cidr)
	}
	sort.Strings(scope.DeniedCIDRs)
	for index, domain := range scope.Domains {
		scope.Domains[index] = normalizeDomain(domain)
		if scope.Domains[index] == "" || scope.Domains[index] == "*" {
			return Scope{}, fmt.Errorf("domain scope must be concrete")
		}
	}
	for index, domain := range scope.DeniedDomains {
		scope.DeniedDomains[index] = normalizeDomain(domain)
	}
	for _, allowed := range scope.Domains {
		for _, denied := range scope.DeniedDomains {
			if denied == "*" || allowed == denied || strings.HasSuffix(allowed, "."+denied) {
				return Scope{}, fmt.Errorf("allowed domain %q is denied", allowed)
			}
		}
	}
	return scope, nil
}

func resolveScope(ctx context.Context, resolver Resolver, scope Scope) (Scope, error) {
	for _, domain := range scope.Domains {
		cidrs, err := resolveDomain(ctx, resolver, domain)
		if err != nil {
			return Scope{}, fmt.Errorf("resolve allowed domain %q: %w", domain, err)
		}
		scope.CIDRs = append(scope.CIDRs, cidrs...)
	}
	for _, domain := range scope.DeniedDomains {
		if domain == "*" {
			continue
		}
		cidrs, err := resolveDomain(ctx, resolver, domain)
		if err != nil {
			return Scope{}, fmt.Errorf("resolve denied domain %q: %w", domain, err)
		}
		scope.DeniedCIDRs = append(scope.DeniedCIDRs, cidrs...)
	}
	scope.CIDRs = uniqueStrings(scope.CIDRs)
	scope.DeniedCIDRs = uniqueStrings(scope.DeniedCIDRs)
	return scope, nil
}

func resolveDomain(ctx context.Context, resolver Resolver, domain string) ([]string, error) {
	addresses, err := resolver.LookupIP(ctx, "ip", domain)
	if err != nil {
		return nil, err
	}
	if len(addresses) == 0 {
		return nil, fmt.Errorf("no addresses returned")
	}
	cidrs := make([]string, 0, len(addresses))
	for _, address := range addresses {
		bits := 128
		if address.To4() != nil {
			address = address.To4()
			bits = 32
		}
		cidrs = append(cidrs, fmt.Sprintf("%s/%d", address.String(), bits))
	}
	return uniqueStrings(cidrs), nil
}

func normalizeDomain(domain string) string {
	return strings.TrimSuffix(strings.ToLower(strings.TrimSpace(domain)), ".")
}

func uniqueStrings(values []string) []string {
	unique := make(map[string]struct{}, len(values))
	for _, value := range values {
		unique[value] = struct{}{}
	}
	result := make([]string, 0, len(unique))
	for value := range unique {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func RenderNftablesPolicy(sandboxID string, scope Scope) string {
	chain := "sb_" + strings.ReplaceAll(sandboxID, "-", "_")
	var rules strings.Builder
	fmt.Fprintf(&rules, "table inet riftx {\n  chain %s {\n    type filter hook output priority 0; policy drop;\n", chain)
	rules.WriteString("    ct state established,related accept\n")
	for _, cidr := range scope.DeniedCIDRs {
		family := "ip"
		if strings.Contains(cidr, ":") {
			family = "ip6"
		}
		fmt.Fprintf(&rules, "    %s daddr %s drop\n", family, cidr)
	}
	for _, cidr := range scope.CIDRs {
		family := "ip"
		if strings.Contains(cidr, ":") {
			family = "ip6"
		}
		if len(scope.Ports) == 0 {
			fmt.Fprintf(&rules, "    %s daddr %s accept\n", family, cidr)
			continue
		}
		for _, port := range scope.Ports {
			fmt.Fprintf(&rules, "    %s daddr %s tcp dport %d accept\n", family, cidr, port)
			fmt.Fprintf(&rules, "    %s daddr %s udp dport %d accept\n", family, cidr, port)
		}
	}
	rules.WriteString("  }\n}\n")
	return rules.String()
}
