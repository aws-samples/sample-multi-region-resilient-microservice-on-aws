package com.amazon.sample.ui.services.orders;

import com.amazon.sample.ui.clients.orders.model.ExistingOrder;
import com.amazon.sample.ui.clients.orders.model.OrderItem;
import com.amazon.sample.ui.services.orders.model.Order;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Instant;
import java.util.Collections;

public class MockOrdersService implements OrdersService{

    @Override
    public Mono<Order> order(String sessionId, String firstName, String lastName, String email) {
        return null;
    }

    @Override
    public Flux<ExistingOrder> fetchOrders() {

        ExistingOrder order = new ExistingOrder();
        order.setId("1");
        order.setEmail("test@xyz.com");
        order.setFirstName("TEST");
        order.setLastName("TEST");
        order.setCreatedOn(Instant.now().toString());
        OrderItem item = new OrderItem();
        item.setPrice(50);
        item.setQuantity(5);
        item.setProductId("123");
        order.addItemsItem(item);
        OrderItem item2 = new OrderItem();
        item2.setPrice(100);
        item2.setQuantity(10);
        item2.setProductId("456");
        order.addItemsItem(item2);
        return Flux.just(order);

    }
}
